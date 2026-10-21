"""
standby_insurance.py — call-out insurance: a consented, paid standby who is
called automatically when somebody does not turn up (scheduler audit
10/10/26 MARKET-26).

schedule_learning.standby_days already names the dates most likely to lose
a person and somebody off that day who could cover. It was a sentence in
the generation preview: nothing committed the person, and when the no-show
happened the coverage check raised an issue 15 minutes late and the manager
started phoning round.

  suggestions()   the week's standby days with their person, each PRICED:
                  the standby pay (the restaurant's standby policy — the
                  owner's own, `standby_policy`: pay_hours at the person's
                  rate, or a flat amount; a jurisdiction's on-call law, where
                  one applies, is counsel's to set and is not invented here)
                  against the expected cost of a short night: the chance of
                  a no-show that date × what this restaurant's short nights
                  measurably cost (`short_night_cost`: same-weekday sales on
                  nights the role ran one short against the weeks around
                  them — stated only when MIN_SHORT_NIGHTS such nights exist
                  and the range excludes no loss — and the guest minutes the
                  service simulator measures for one fewer of the family).
  offer()         the owner turns a suggestion into an offered on-call slot:
                  the person must be one the replacement ranking holds legal
                  for that day; they are told in the app (people.tell).
  respond()       the person's answer from the staff app: a yes is written,
                  time-stamped consent to the exact terms shown (pay, hours,
                  "you may be called in") — `consent_at`, `consent_text`,
                  `consent_channel` — and it can be withdrawn until the
                  shift starts.
  dispatch_for()  called by strategy_jobs._raise_coverage the moment a
                  coverage issue names somebody missing: an accepted standby
                  for that business date and role family is added to the
                  issue's covers and offered the shift at once
                  (intraday.ask_to_cover → shift_requests.post_open_shift
                  with offer_to — the claim's full legality sweep runs again
                  then). The slot records the offer; its outcome is the
                  offer's.
  settle()        after the shift: a standby never called in is owed the
                  standby pay (`pay_owed`); one called in is paid for the
                  shift they worked through the punches, and the slot says so.
                  A slot the owner withdrew after the person agreed is owed
                  too. The owner's list carries every slot's pay for payroll.

Owner-facing (studio_bench 'standby'); staff see their own offers
(/staff/api/standby). Nothing calls a model.
"""
import logging
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

DEFAULT_PAY_HOURS = 2.0           # an unstated policy: two hours at the person's rate for being on call
MIN_SHORT_NIGHTS = 4              # nights a role ran short before a sales cost is stated
SHORT_LOOKBACK_DAYS = 365
STATUSES = ("offered", "accepted", "declined", "withdrawn", "dispatched", "released", "expired")
CONSENT_VERSION = 1



def _models_db():
    """The database path as models has it now (CLAUDE.md, bound imports: a
    module's own DB_PATH is the one it was imported with)."""
    import models
    return models.DB_PATH

def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def init_standby(db_path=None):
    """Boot DDL (models.init_db): the slots and the owner's policy."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS standby_slots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            history_id      INTEGER,
            date            TEXT    NOT NULL,
            role            TEXT    NOT NULL,
            family          TEXT,
            shift_start     TEXT    NOT NULL,
            shift_end       TEXT    NOT NULL,
            employee_name   TEXT    NOT NULL,
            name_key        TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'offered',
            standby_pay     REAL,
            pay_basis       TEXT,
            chance          REAL,
            expected_cost   REAL,
            terms           TEXT,
            offered_by      TEXT,
            offered_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            consent_at      TEXT,
            consent_text    TEXT,
            consent_channel TEXT,
            consent_version INTEGER,
            answered_at     TEXT,
            dispatched_at   TEXT,
            issue_id        INTEGER,
            offer_id        INTEGER,
            dispatch_error  TEXT,
            settled_at      TEXT,
            pay_owed        REAL,
            outcome         TEXT,
            UNIQUE(restaurant_id, date, name_key, shift_start)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_standby_day ON standby_slots(restaurant_id, date, status)")
        conn.execute("""CREATE TABLE IF NOT EXISTS standby_policy (
            restaurant_id   INTEGER PRIMARY KEY REFERENCES restaurants(id),
            pay_hours       REAL,
            flat_pay        REAL,
            note            TEXT,
            updated_by      TEXT,
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.commit()
    finally:
        conn.close()


class StandbyError(ValueError):
    """A refusal the caller shows as it is."""


def _key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _family(restaurant_id, role, db_path=None):
    from shift_quality import role_family
    try:
        import models
        import schedule_rules
        fams = schedule_rules.role_families(models.get_restaurant(restaurant_id, db_path or models.DB_PATH)) or {}
    except Exception:
        fams = {}
    return role_family(role or "", fams) or ""


# ── the price ──────────────────────────────────────────────────────────────

def policy(restaurant_id, db_path=None) -> dict:
    """{pay_hours, flat_pay, note, stated}: the owner's standby policy, or
    the default (stated False) — said as the default wherever it is used."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM standby_policy WHERE restaurant_id=?", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return {"pay_hours": DEFAULT_PAY_HOURS, "flat_pay": None, "note": None, "stated": False}
    return {"pay_hours": r["pay_hours"], "flat_pay": r["flat_pay"], "note": r["note"], "stated": True}


def set_policy(restaurant_id, pay_hours=None, flat_pay=None, note=None, by=None, db_path=None) -> dict:
    def _num(v, lo, hi):
        if v in (None, ""):
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise StandbyError("standby pay must be a number")
        if not lo <= v <= hi:
            raise StandbyError(f"standby pay must be between {lo:g} and {hi:g}")
        return v
    ph, fp = _num(pay_hours, 0, 12), _num(flat_pay, 0, 1000)
    if ph is None and fp is None:
        raise StandbyError("say the hours of pay or a flat amount for being on call")
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO standby_policy (restaurant_id, pay_hours, flat_pay, note, updated_by, updated_at) "
                     "VALUES (?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id) DO UPDATE SET "
                     "pay_hours=excluded.pay_hours, flat_pay=excluded.flat_pay, note=excluded.note, "
                     "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                     (restaurant_id, ph, fp, (note or "")[:200] or None, (by or "")[:120] or None))
        conn.commit()
    finally:
        conn.close()
    return policy(restaurant_id, db_path=db_path)


def _rate(restaurant_id, name, role):
    try:
        import labor
        from models import get_role_rates
        import schedule_objective as _obj
        people, roles = labor.person_rate_book(restaurant_id)
        # The forward chain (audit #23 A3-02): the owner's rate for the
        # person, for the role, their pay in this job, in any job, the
        # role's typical.
        r = _obj.forward_rate_of(name, role, get_role_rates(restaurant_id) or {}, 0.0, people, roles)
        return float(r) if r else float(labor.get_hourly_rate(restaurant_id))
    except Exception:
        return None


def standby_pay(restaurant_id, name, role, db_path=None) -> tuple:
    """(dollars or None, basis sentence)."""
    p = policy(restaurant_id, db_path=db_path)
    if p.get("flat_pay") is not None:
        return round(float(p["flat_pay"]), 2), f"${p['flat_pay']:.0f} for being on call (your standby policy)"
    rate = _rate(restaurant_id, name, role)
    hours = float(p.get("pay_hours") or DEFAULT_PAY_HOURS)
    if rate is None:
        return None, f"{hours:g} hours of their pay for being on call — no rate on file to price it"
    basis = (f"{hours:g} hours at their ${rate:.2f} rate for being on call"
             + ("" if p.get("stated") else " (the default until you set a standby policy)"))
    return round(hours * rate, 2), basis


def short_night_cost(restaurant_id, role, weekday, today=None, db_path=None) -> dict:
    """What a night this role ran one short measurably cost here: {sales:
    {loss, lo, hi, nights} | None, guest: {...} | None, reason}. Sales:
    each short night's sales against the median of the same weekday in the
    4 weeks either side (nights not short), the loss share's median and its
    middle 80%, × that weekday's median sales; stated only for
    MIN_SHORT_NIGHTS nights and when the range does not include no loss.
    Guest: the service simulator's one-fewer replay for the role's family."""
    today = today or date.today()
    fam = _family(restaurant_id, role, db_path)
    since = (today - timedelta(days=SHORT_LOOKBACK_DAYS)).isoformat()
    conn = get_conn(db_path)
    try:
        ev = conn.execute("SELECT business_date, role, employee_name FROM attendance_events WHERE restaurant_id=? AND "
                          "business_date>=? AND outcome IN ('no_show','called_out') AND COALESCE(covered_by,'')=''",
                          (restaurant_id, since)).fetchall()
        sales = {str(r["date"])[:10]: float(r["sales"] or 0) for r in conn.execute(
            "SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND date>=? AND sales>0",
            (restaurant_id, since)).fetchall()}
    except Exception as e:
        log.warning("short night cost unreadable rid=%s: %s", restaurant_id, e)
        ev, sales = [], {}
    finally:
        conn.close()
    short = sorted({str(e["business_date"])[:10] for e in ev
                    if _family(restaurant_id, e["role"] or role, db_path) == fam})
    shares = []
    for d in short:
        try:
            dd = date.fromisoformat(d)
        except ValueError:
            continue
        if weekday and dd.strftime("%A") != weekday:
            continue
        if d not in sales:
            continue
        around = [sales.get((dd + timedelta(weeks=k)).isoformat()) for k in (-4, -3, -2, -1, 1, 2, 3, 4)]
        around = sorted(v for v, k in zip(around, (-4, -3, -2, -1, 1, 2, 3, 4))
                        if v and (dd + timedelta(weeks=k)).isoformat() not in short)
        if len(around) < 3:
            continue
        med = around[len(around) // 2]
        shares.append(1.0 - sales[d] / med)
    out = {"role": role, "weekday": weekday, "sales": None, "guest": None}
    wd_sales = sorted(v for d, v in sales.items() if not weekday or date.fromisoformat(d).strftime("%A") == weekday)
    if len(shares) >= MIN_SHORT_NIGHTS and wd_sales:
        s = sorted(shares)
        lo, mid, hi = s[int(0.1 * (len(s) - 1))], s[len(s) // 2], s[int(round(0.9 * (len(s) - 1)))]
        typical = wd_sales[len(wd_sales) // 2]
        if lo > 0:
            out["sales"] = {"loss": round(mid * typical, 0), "lo": round(lo * typical, 0), "hi": round(hi * typical, 0),
                            "nights": len(shares)}
        else:
            out["sales_note"] = (f"{len(shares)} short {weekday or ''} nights show no sales loss that can be told "
                                 "apart from an ordinary night.").replace("  ", " ")
    else:
        out["sales_note"] = (f"Not stated: {len(shares)} short night{'s' if len(shares) != 1 else ''} on record "
                             f"(needs {MIN_SHORT_NIGHTS}, from nights the clock-in check or the closeout saw).")
    try:
        import service_sim
        if weekday:
            w = service_sim.what_if(restaurant_id, weekday, {fam or role: -1}, today=today, db_path=db_path)
            if w.get("available") and w.get("sentence"):
                out["guest"] = {"sentence": w["sentence"], "stages": w.get("stages"), "review_risk": w.get("review_risk")}
    except Exception as e:
        log.warning("short night guest cost unavailable rid=%s: %s", restaurant_id, e)
    return out


def _priced(restaurant_id, day, today=None, db_path=None) -> dict:
    """One standby day (schedule_learning.standby_days) with its price."""
    sb = day.get("standby") or {}
    if not sb.get("employee"):
        return dict(day, price=None)
    pay, basis = standby_pay(restaurant_id, sb["employee"], sb.get("role"), db_path=db_path)
    wd = date.fromisoformat(day["date"]).strftime("%A")
    cost = short_night_cost(restaurant_id, sb.get("role"), wd, today=today, db_path=db_path)
    expected = None
    if cost.get("sales"):
        expected = round(day["chance_of_a_no_show"] * cost["sales"]["loss"], 0)
    verdict = None
    if pay is not None and expected is not None:
        verdict = ("worth it on sales alone" if expected > pay else
                   "costs more than the sales a short night loses here — worth it only for the guests")
    return dict(day, price={"standby_pay": pay, "pay_basis": basis, "expected_short_cost": expected,
                            "short_night": cost, "verdict": verdict})


# ── the slots ──────────────────────────────────────────────────────────────

def _row(r) -> dict:
    d = dict(r)
    return d


def slots(restaurant_id, since=None, until=None, name=None, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        q, args = "SELECT * FROM standby_slots WHERE restaurant_id=?", [restaurant_id]
        if since:
            q += " AND date>=?"
            args.append(since)
        if until:
            q += " AND date<=?"
            args.append(until)
        if name:
            q += " AND name_key=?"
            args.append(_key(name))
        rows = conn.execute(q + " ORDER BY date, shift_start", args).fetchall()
    finally:
        conn.close()
    out = [_row(r) for r in rows]
    _attach_offer_outcomes(restaurant_id, out, db_path)
    return out


def _attach_offer_outcomes(restaurant_id, rows, db_path=None):
    ids = [r["offer_id"] for r in rows if r.get("offer_id")]
    if not ids:
        return
    conn = get_conn(db_path)
    try:
        got = {o["id"]: o["status"] for o in conn.execute(
            f"SELECT id, status FROM shift_offers WHERE restaurant_id=? AND id IN ({','.join('?' for _ in ids)})",
            [restaurant_id] + ids).fetchall()}
    except Exception:
        got = {}
    finally:
        conn.close()
    for r in rows:
        if r.get("offer_id"):
            r["offer_status"] = got.get(r["offer_id"])
            r["called_in"] = got.get(r["offer_id"]) == "accepted"


def suggestions(restaurant_id, week_dates, rows=None, today=None, db_path=None) -> list:
    """The week's standby days, priced, each with the slot already offered
    for it (if any)."""
    import schedule_learning
    try:
        days = schedule_learning.standby_days(restaurant_id, list(week_dates or []), rows=rows, limit=3,
                                              db_path=db_path or _models_db())
    except Exception as e:
        log.warning("standby suggestions unavailable rid=%s: %s", restaurant_id, e)
        return []
    have = {}
    if week_dates:
        for s in slots(restaurant_id, since=min(week_dates), until=max(week_dates), db_path=db_path):
            have.setdefault(s["date"], []).append(s)
    return [dict(_priced(restaurant_id, d, today=today, db_path=db_path), slots=have.get(d["date"], [])) for d in days]


def terms_text(place, day, role, start, end, pay, basis) -> str:
    """The exact words a person agrees to (kept with their yes)."""
    from time_utils import mdy
    paid = f"${pay:.2f}" if pay is not None else "standby pay"
    return (f"I agree to be on call at {place} on {date.fromisoformat(day).strftime('%A')} {mdy(day)} for the "
            f"{role} shift {start}–{end}. If I am not called in I am paid {paid} for being available ({basis}). "
            "If I am called in, I am paid for the shift I work. I can withdraw before the shift starts.")


def offer(restaurant_id, day, role, employee, shift_start, shift_end, by=None, history_id=None, chance=None,
          expected_cost=None, today=None, db_path=None) -> dict:
    """The owner's offer of an on-call slot (module doc). Raises
    StandbyError."""
    import models
    import schedule_rules
    name = " ".join(str(employee or "").split())
    if not name or not (role or "").strip():
        raise StandbyError("who and which role?")
    try:
        d = date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        raise StandbyError("that is not a date")
    if d < (today or date.today()):
        raise StandbyError("that day has passed")
    if schedule_rules.parse_minutes(shift_start or "") is None or schedule_rules.parse_minutes(shift_end or "") is None:
        raise StandbyError("give the on-call hours, like 5:00pm to 10:00pm")
    # Legal for that day: somebody the replacement ranking holds free, in the
    # role and not on time off (the claim's full sweep runs again at dispatch).
    try:
        import labor_replacements
        fits = labor_replacements.for_gap(restaurant_id, role, d.strftime("%A"), db_path=db_path or models.DB_PATH,
                                          limit=50, on_date=d.isoformat())
    except Exception as e:
        log.warning("standby legality unreadable rid=%s: %s", restaurant_id, e)
        fits = []
    if not any(_key(f.get("name")) == _key(name) for f in fits):
        raise StandbyError(f"{name} can't be on call then: they are not free in that role that day")
    pay, basis = standby_pay(restaurant_id, name, role, db_path=db_path)
    r = models.get_restaurant(restaurant_id, db_path or models.DB_PATH)
    place = (getattr(r, "location_name", None) or getattr(r, "name", None) or "the restaurant") if r else "the restaurant"
    terms = terms_text(place, d.isoformat(), role, shift_start, shift_end, pay, basis)
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO standby_slots (restaurant_id, history_id, date, role, family, shift_start, shift_end, "
            "employee_name, name_key, status, standby_pay, pay_basis, chance, expected_cost, terms, offered_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,'offered',?,?,?,?,?,?) ON CONFLICT(restaurant_id, date, name_key, shift_start) "
            "DO UPDATE SET status='offered', role=excluded.role, family=excluded.family, shift_end=excluded.shift_end, "
            "standby_pay=excluded.standby_pay, pay_basis=excluded.pay_basis, chance=excluded.chance, "
            "expected_cost=excluded.expected_cost, terms=excluded.terms, offered_by=excluded.offered_by, "
            "offered_at=datetime('now'), consent_at=NULL, consent_text=NULL, consent_channel=NULL, answered_at=NULL "
            "WHERE standby_slots.status IN ('declined','withdrawn','expired')",
            (restaurant_id, history_id, d.isoformat(), role.strip(), _family(restaurant_id, role, db_path),
             shift_start, shift_end, name, _key(name), pay, basis, chance, expected_cost, terms,
             (by or "")[:120] or None))
        conn.commit()
        if cur.rowcount == 0:
            raise StandbyError(f"{name} already has an on-call slot then")
        row = conn.execute("SELECT * FROM standby_slots WHERE restaurant_id=? AND date=? AND name_key=? AND "
                           "shift_start=?", (restaurant_id, d.isoformat(), _key(name), shift_start)).fetchone()
    finally:
        conn.close()
    slot = _row(row)
    try:
        import people
        from time_utils import mdy
        slot["via"] = people.tell(
            restaurant_id, name, f"Can you be on call {d.strftime('%A')} {mdy(d.isoformat())}?",
            [f"You'd be on call for the {role} shift {shift_start}–{shift_end}"
             + (f", paid ${pay:.2f} for being available" if pay is not None else "")
             + ". If someone can't make it, you're called in first. Answer in the app under Requests."],
            kind="request", nav={"tab": "requests", "standby_id": slot["id"]}, shift_date=d.isoformat(),
            db_path=db_path)
    except Exception as e:
        log.warning("standby offer notice failed rid=%s: %s", restaurant_id, e)
        slot["via"] = None
    return slot


def respond(restaurant_id, slot_id, name, accept, agree=False, channel="app", db_path=None) -> dict:
    """The person's answer. A yes needs `agree` — their explicit consent to
    the terms they were shown — and is time-stamped with those words."""
    if accept and not agree:
        raise StandbyError("tick that you agree to the on-call terms to say yes")
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM standby_slots WHERE id=? AND restaurant_id=?", (int(slot_id), restaurant_id)).fetchone()
        if not r or r["name_key"] != _key(name):
            raise StandbyError("that on-call offer is not yours")
        if r["status"] not in ("offered", "accepted"):
            raise StandbyError("that on-call offer is closed")
        if accept:
            conn.execute("UPDATE standby_slots SET status='accepted', consent_at=?, consent_text=terms, "
                         "consent_channel=?, consent_version=?, answered_at=? WHERE id=?",
                         (_now(), (channel or "app")[:20], CONSENT_VERSION, _now(), r["id"]))
        else:
            # A withdrawal of consent is time-stamped too; before the shift only.
            conn.execute("UPDATE standby_slots SET status=CASE WHEN status='accepted' THEN 'withdrawn' ELSE "
                         "'declined' END, answered_at=? WHERE id=?", (_now(), r["id"]))
        conn.commit()
        row = conn.execute("SELECT * FROM standby_slots WHERE id=?", (r["id"],)).fetchone()
    finally:
        conn.close()
    slot = _row(row)
    try:
        import people
        from time_utils import mdy
        people.tell_deciders(restaurant_id, f"{slot['employee_name']} {'is on call' if accept else 'can’t be on call'}"
                             f" {mdy(slot['date'])}",
                             f"{slot['role']} {slot['shift_start']}–{slot['shift_end']}", alert_type="shift_request")
    except Exception as e:
        log.warning("standby answer notice failed rid=%s: %s", restaurant_id, e)
    return slot


def withdraw(restaurant_id, slot_id, by=None, db_path=None) -> dict:
    """The owner takes the slot back. After a yes the standby pay is still
    owed (they kept the night free)."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM standby_slots WHERE id=? AND restaurant_id=?", (int(slot_id), restaurant_id)).fetchone()
        if not r or r["status"] not in ("offered", "accepted"):
            raise StandbyError("that on-call slot is not open")
        owed = r["standby_pay"] if r["status"] == "accepted" else None
        conn.execute("UPDATE standby_slots SET status='withdrawn', pay_owed=?, outcome=?, settled_at=? WHERE id=?",
                     (owed, "withdrawn by the restaurant after they agreed" if owed is not None else "withdrawn",
                      _now() if owed is not None else None, r["id"]))
        conn.commit()
        return _row(conn.execute("SELECT * FROM standby_slots WHERE id=?", (r["id"],)).fetchone())
    finally:
        conn.close()


def for_staff(restaurant_id, name, today=None, db_path=None) -> list:
    """The person's own on-call offers and slots from today on (no other
    person, no expected-cost figures — those are the owner's)."""
    today = today or date.today()
    out = []
    for s in slots(restaurant_id, since=today.isoformat(), name=name, db_path=db_path):
        out.append({k: s.get(k) for k in ("id", "date", "role", "shift_start", "shift_end", "status", "standby_pay",
                                          "terms", "consent_at", "called_in")})
    return out


# ── the dispatch ───────────────────────────────────────────────────────────

def dispatch_for(restaurant_id, business_date, missing, issue_id, db_path=None) -> list:
    """The coverage check's hook (strategy_jobs._raise_coverage): for each
    person `missing` ([{employee, role, shift_start}]), the accepted standby
    of that date and role family not yet dispatched is put on the issue's
    covers and offered the shift at once. Returns [{slot_id, name, ok, via
    | error}]. Never raises."""
    out = []
    try:
        ready = [s for s in slots(restaurant_id, since=business_date, until=business_date, db_path=db_path)
                 if s["status"] == "accepted"]
    except Exception as e:
        log.warning("standby dispatch unreadable rid=%s: %s", restaurant_id, e)
        return out
    if not ready:
        return out
    import intraday
    import issues
    for m in missing or []:
        fam = _family(restaurant_id, m.get("role"), db_path)
        slot = next((s for s in ready if (s.get("family") or _family(restaurant_id, s["role"], db_path)) == fam), None)
        if slot is None:
            continue
        ready.remove(slot)
        cover = {"name": slot["employee_name"], "for": m.get("employee"), "shift_start": m.get("shift_start"),
                 "standby": True, "reason": "on call for tonight (agreed " + str(slot.get("consent_at") or "")[:16] + ")"}

        def _add(meta, cover=cover):
            # First among the covers, marked as the standby — a suggestion
            # the check already made for them is that same entry, moved up.
            covers = meta.setdefault("covers", [])
            have = [c for c in covers if _key(c.get("name")) == _key(cover["name"])]
            if have and have[0].get("standby"):
                return False
            for c in have:
                covers.remove(c)
            covers.insert(0, dict(have[0], **cover) if have else cover)
            return True
        try:
            issues.update_coverage(restaurant_id, issue_id, _add, db_path=db_path)
        except Exception as e:
            log.warning("standby cover not added rid=%s: %s", restaurant_id, e)
        try:
            got = intraday.ask_to_cover(restaurant_id, issue_id, slot["employee_name"], surface="standby",
                                        db_path=db_path or _models_db(), actor="Cavnar AI (standby)")
        except Exception as e:
            got = {"ok": False, "error": str(e)}
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE standby_slots SET status='dispatched', dispatched_at=?, issue_id=?, offer_id=?, "
                         "dispatch_error=? WHERE id=?",
                         (_now(), issue_id, got.get("offer_id"), None if got.get("ok") else str(got.get("error"))[:300],
                          slot["id"]))
            conn.commit()
        finally:
            conn.close()
        out.append({"slot_id": slot["id"], "name": slot["employee_name"], "ok": bool(got.get("ok")),
                    "via": got.get("via"), "error": got.get("error")})
    return out


def settle(restaurant_id, today=None, db_path=None) -> int:
    """Slots whose day has passed: accepted and never dispatched → released,
    the standby pay owed; dispatched → the offer's outcome (called in: paid
    through the punches, nothing extra owed by this policy; not taken:
    standby pay owed); offered and never answered → expired. Returns the
    slots settled."""
    today = today or date.today()
    n = 0
    rows = slots(restaurant_id, until=(today - timedelta(days=1)).isoformat(), db_path=db_path)
    conn = get_conn(db_path)
    try:
        for s in rows:
            if s.get("settled_at"):
                continue
            if s["status"] == "accepted":
                vals = ("released", s["standby_pay"], "on call, not needed")
            elif s["status"] == "dispatched":
                vals = ("dispatched", None if s.get("called_in") else s["standby_pay"],
                        "called in and worked the shift" if s.get("called_in") else "called, did not take the shift")
            elif s["status"] == "offered":
                vals = ("expired", None, "never answered")
            else:
                continue
            conn.execute("UPDATE standby_slots SET status=?, pay_owed=?, outcome=?, settled_at=? WHERE id=?",
                         vals + (_now(), s["id"]))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def owner_view(restaurant_id, week_dates=None, rows=None, today=None, db_path=None) -> dict:
    """The Studio's standby panel: the week's priced suggestions, every
    slot of the last 4 weeks and the coming ones with consent and pay, and
    the policy."""
    today = today or date.today()
    settle(restaurant_id, today=today, db_path=db_path)
    out = {"ok": True, "policy": policy(restaurant_id, db_path=db_path),
           "suggestions": suggestions(restaurant_id, week_dates, rows=rows, today=today, db_path=db_path)
           if week_dates else [],
           "slots": slots(restaurant_id, since=(today - timedelta(weeks=4)).isoformat(), db_path=db_path)}
    owed = [s for s in out["slots"] if s.get("pay_owed")]
    out["pay_owed"] = round(sum(float(s["pay_owed"]) for s in owed), 2) if owed else 0.0
    return out
