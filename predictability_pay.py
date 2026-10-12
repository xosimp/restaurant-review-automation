"""
predictability_pay.py — what a change to a posted week costs under a
predictive-scheduling law, per person and per shift, before it is sent and
on the record after.

A change to a published week inside the notice window used to carry one
sentence — "the restaurant may owe the affected staff premium pay ... check
with counsel" — and the product "did not model predictability pay"
(schedule audit 10/10/26 MARKET-2). A chain cannot run payroll from "may
owe". Now:

  classify(before, after)   each person's changed shifts: add, extend, move
                            (no hours lost), reduce, cut — with the minutes
                            and hours moved
  price(...)                the law's premium for each (compliance_packs:
                            the ordinance that COVERS the restaurant, its
                            tiers by notice, its minimum-change threshold,
                            its exemptions: a trade, a change the employee
                            asked for, written consent where that law
                            accepts it), the person's own rate, and whether
                            they are a covered employee at all
  preview(...)              the owner's view before Send (the edit, the
                            publish review), priced, nothing written
  record_send(...)          the ledger rows written when the change is sent
  record_request_change(...)  a cover or swap the staff made themselves —
                            on the record, exempt as employee-initiated
  record_rest_premiums(...) the rest premium a consented clopen owes, when
                            its week is sent
  export_csv(...)           the ledger for payroll

Ledger: `compliance_pay_ledger`, one row per changed shift (or premium
instance), kept three years with the scheduling records (ops retention).
Nothing here sends anything. A restaurant no predictive law covers gets an
empty preview and no rows — and the owner's own notice rule, without a law
behind it, still gets the old possibility-to-check sentence
(schedule_rules.late_change_warning), never a sum.
"""
import csv
import io
import json
from datetime import datetime, timedelta

from models import DB_PATH
import models as _models

KINDS = ("add", "extend", "move", "reduce", "cut", "rest_premium")
STATUSES = ("owed", "exempt", "not_covered", "unknown_coverage")


def get_conn(db_path=None):
    return _models.get_conn(db_path or DB_PATH)


def init_compliance_pay(db_path=None):
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS compliance_pay_ledger (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            history_id      INTEGER,
            version         INTEGER,
            jurisdiction    TEXT,
            person_id       INTEGER,
            employee_name   TEXT    NOT NULL,
            employee_key    TEXT    NOT NULL,
            shift_date      TEXT    NOT NULL,
            change_kind     TEXT    NOT NULL,
            before_json     TEXT,
            after_json      TEXT,
            minutes_changed INTEGER,
            hours_lost      REAL,
            hours_added     REAL,
            notice_hours    REAL,
            initiator       TEXT    NOT NULL DEFAULT 'employer',
            exempt_reason   TEXT,
            consent_id      INTEGER,
            premium_hours   REAL,
            rate            REAL,
            amount          REAL    NOT NULL DEFAULT 0,
            rule_json       TEXT,
            source          TEXT,
            status          TEXT    NOT NULL,
            sent_at         TEXT    NOT NULL,
            sent_by         TEXT,
            exported_at     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cpl_restaurant_date ON compliance_pay_ledger"
                     "(restaurant_id, shift_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cpl_created ON compliance_pay_ledger(created_at)")
        conn.commit()
    finally:
        conn.close()


def _key(name):
    return " ".join(str(name or "").lower().split())


def _min(label):
    from schedule_rules import parse_minutes
    return parse_minutes(label or "")


def _hours(r):
    s, e = _min(r.get("shift_start")), _min(r.get("shift_end"))
    if s is None or e is None:
        return 0.0
    if e <= s:
        e += 24 * 60
    return (e - s) / 60.0


def _start_dt(r):
    m = _min(r.get("shift_start"))
    try:
        d = datetime.strptime(str(r.get("date"))[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    if m is None:
        return d
    return d + timedelta(minutes=m)


def _shift(r):
    return {"date": r.get("date"), "start": r.get("shift_start"), "end": r.get("shift_end"),
            "role": r.get("role"), "hours": round(_hours(r), 2)}


def _overlap(a, b):
    sa, ea = _min(a.get("shift_start")), _min(a.get("shift_end"))
    sb, eb = _min(b.get("shift_start")), _min(b.get("shift_end"))
    if None in (sa, ea, sb, eb):
        return 0
    if ea <= sa:
        ea += 24 * 60
    if eb <= sb:
        eb += 24 * 60
    return max(0, min(ea, eb) - max(sa, sb))


# ── classify ───────────────────────────────────────────────────────────────

def classify(before_rows, after_rows, restaurant_id=None, db_path=None) -> list:
    """Each person's changed shifts between two versions of a week.

    [{"employee", "key", "date", "kind", "before", "after", "minutes_changed",
      "start_shift_minutes", "end_shift_minutes", "hours_lost", "hours_added",
      "shift_hours", "first_start"}] — kind: add (a new shift), extend
    (longer, nothing lost), move (time or date changed, no hours lost),
    reduce (hours lost), cut (cancelled). A shift cut on one date and one of
    at least its hours added on another, for the same person in the same
    week, is one move of date.

    With `restaurant_id`, both weeks' names are read as the one person each
    means now (people.canonical_names, as unsent_changes reads them —
    schedule re-audit 10/11/26 BUGSLIFE-6): a rename between two Sends is
    never priced as a "cut" of the old spelling and an "add" of the new."""
    if restaurant_id is not None:
        try:
            import schedule_versions as _sv_pp
            before_rows, after_rows = _sv_pp.canonical_rows(restaurant_id, [before_rows or [], after_rows or []],
                                                           db_path=db_path)
        except Exception as e:
            print(f"[predictability_pay] names not read as people rid={restaurant_id}: {e!r}")

    def norm(rows):
        out = {}
        for r in rows or []:
            k = _key(r.get("employee"))
            if not k or not r.get("date"):
                continue
            out.setdefault(k, []).append(r)
        return out

    b_by, a_by = norm(before_rows), norm(after_rows)
    names = {}
    for r in list(before_rows or []) + list(after_rows or []):
        if _key(r.get("employee")):
            names.setdefault(_key(r.get("employee")), " ".join(str(r.get("employee")).split()))
    out = []
    for k in sorted(set(b_by) | set(a_by)):
        before = list(b_by.get(k, []))
        after = list(a_by.get(k, []))
        same = lambda x, y: (x.get("date") == y.get("date") and x.get("shift_start") == y.get("shift_start")
                             and x.get("shift_end") == y.get("shift_end"))
        # unchanged shifts drop out
        for x in list(before):
            m = next((y for y in after if same(x, y)), None)
            if m is not None:
                before.remove(x)
                after.remove(m)
        pairs = []
        for x in sorted(before, key=lambda r: (r.get("date"), _min(r.get("shift_start")) or 0)):
            cands = [y for y in after if y.get("date") == x.get("date")]
            if not cands:
                continue
            y = max(cands, key=lambda c: (_overlap(x, c), -abs((_min(c.get("shift_start")) or 0)
                                                               - (_min(x.get("shift_start")) or 0))))
            pairs.append((x, y))
            after.remove(y)
        for x, _y in pairs:
            before.remove(x)
        # a cut on one date and an add of at least its hours on another:
        # one move of the shift's date
        for x in sorted(list(before), key=lambda r: r.get("date")):
            y = next((c for c in sorted(after, key=lambda r: r.get("date")) if _hours(c) >= _hours(x) - 1e-6), None)
            if y is not None:
                pairs.append((x, y))
                before.remove(x)
                after.remove(y)
        for x, y in pairs:
            hb, ha = _hours(x), _hours(y)
            ds = abs((_min(y.get("shift_start")) or 0) - (_min(x.get("shift_start")) or 0))
            de = abs((_min(y.get("shift_end")) or 0) - (_min(x.get("shift_end")) or 0))
            if x.get("date") != y.get("date"):
                ds = de = int(round(max(hb, ha) * 60))
            if ha < hb - 1e-6:
                kind = "reduce"
            elif x.get("date") == y.get("date") and (_min(y.get("shift_start")) or 0) <= (_min(x.get("shift_start")) or 0) \
                    and _overlap(x, y) >= int(round(hb * 60)) - 1:
                kind = "extend" if ha > hb + 1e-6 else "move"
            else:
                kind = "move"
            starts = [d for d in (_start_dt(x), _start_dt(y)) if d]
            out.append({"employee": names.get(k, k), "key": k, "date": y.get("date") if kind != "reduce" else x.get("date"),
                        "kind": kind, "before": _shift(x), "after": _shift(y), "minutes_changed": int(ds + de),
                        "start_shift_minutes": int(ds), "end_shift_minutes": int(de),
                        "hours_lost": round(max(0.0, hb - ha), 2), "hours_added": round(max(0.0, ha - hb), 2),
                        "shift_hours": round(hb, 2), "first_start": min(starts) if starts else None})
        for x in before:
            out.append({"employee": names.get(k, k), "key": k, "date": x.get("date"), "kind": "cut",
                        "before": _shift(x), "after": None, "minutes_changed": int(round(_hours(x) * 60)),
                        "start_shift_minutes": None, "end_shift_minutes": None,
                        "hours_lost": round(_hours(x), 2), "hours_added": 0.0, "shift_hours": round(_hours(x), 2),
                        "first_start": _start_dt(x)})
        for y in after:
            out.append({"employee": names.get(k, k), "key": k, "date": y.get("date"), "kind": "add",
                        "before": None, "after": _shift(y), "minutes_changed": int(round(_hours(y) * 60)),
                        "start_shift_minutes": None, "end_shift_minutes": None,
                        "hours_lost": 0.0, "hours_added": round(_hours(y), 2), "shift_hours": round(_hours(y), 2),
                        "first_start": _start_dt(y)})
    out.sort(key=lambda c: (c["date"] or "", c["employee"]))
    return out


# ── price ──────────────────────────────────────────────────────────────────

def _tier_for(spec, kind, notice_hours):
    for t in spec.get("tiers") or []:
        if kind not in (t.get("kinds") or []):
            continue
        if notice_hours >= float(t.get("under_hours") or 0):
            continue
        if notice_hours < float(t.get("at_least_hours") or 0):
            continue
        return t
    return None


def pay_hours(pay: dict, change: dict, rate):
    """(premium_hours or None, amount or None) for one tier's pay."""
    if "flat" in pay:
        return None, float(pay["flat"])
    if rate in (None, 0):
        rate = None
    if "hours" in pay:
        h = float(pay["hours"])
    elif "fraction_of_lost" in pay:
        h = float(pay["fraction_of_lost"]) * float(change.get("hours_lost") or 0)
    elif "hours_by_shift" in pay:
        sh = float(change.get("shift_hours") or 0)
        h = 0.0
        for upto, n in pay["hours_by_shift"]:
            if upto is None or sh <= float(upto) + 1e-6:
                h = float(n)
                break
    elif "min_hours_of" in pay:
        other = change.get("shift_hours") if pay.get("or") == "shift_hours" else change.get("hours_lost")
        h = min(float(pay["min_hours_of"]), float(other or 0))
    else:
        return None, None
    return round(h, 2), (round(h * float(rate), 2) if rate is not None else None)


def price(changes, spec: dict, sent_at, rate_of=None, covered_of=None, consent_of=None, initiator="employer",
          code=None, source=None, on=None) -> list:
    """Each change inside the notice window, priced by the law's `spec`
    (compliance_packs predictability_pay value). Changes with notice at or
    past the window are not returned — nothing is owed and nothing to
    record. `rate_of(name, role)` -> hourly rate or None; `covered_of(name,
    rate)` -> True | False | None; `consent_of(change)` -> a staff_consent
    row or None; `initiator` "employer" | "employee"."""
    out = []
    window_h = float(spec.get("window_days") or 14) * 24
    thr = spec.get("min_change_minutes")
    for c in changes or []:
        start = c.get("first_start")
        if start is None:
            continue
        notice = (start - sent_at).total_seconds() / 3600.0
        if notice >= window_h:
            continue
        role = ((c.get("after") or c.get("before") or {}).get("role") or "")
        rate = rate_of(c["employee"], role) if rate_of else None
        tr = spec.get("tipped_rate")
        if tr and rate is not None and float(rate) < float(tr.get("below") or 0):
            d = str(c.get("date") or "")[:10]
            if (not tr.get("from") or d >= tr["from"]) and (not tr.get("until") or d < tr["until"]):
                rate = float(tr["rate"])
        row = dict(c, notice_hours=round(notice, 2), rate=rate, initiator=initiator, jurisdiction=code,
                   source=source, exempt_reason=None, consent_id=None, premium_hours=None, amount=0.0,
                   status="owed", rule=None)
        row.pop("first_start", None)
        covered = covered_of(c["employee"], rate) if covered_of else True
        if covered is False:
            row["status"] = "not_covered"
            row["exempt_reason"] = "not a covered employee under this law"
            out.append(row)
            continue
        if initiator == "employee":
            row["status"] = "exempt"
            row["exempt_reason"] = "the employee's own request or a trade between employees"
            out.append(row)
            continue
        if thr and c["kind"] in ("move", "extend", "reduce") and c.get("start_shift_minutes") is not None \
                and c["start_shift_minutes"] <= thr and c["end_shift_minutes"] <= thr:
            row["status"] = "exempt"
            row["exempt_reason"] = f"a start or end moved {int(thr)} minutes or less"
            out.append(row)
            continue
        if spec.get("consent_exempts") and consent_of:
            consent = consent_of(c)
            if consent:
                row["status"] = "exempt"
                row["consent_id"] = consent.get("id")
                row["exempt_reason"] = f"agreed in writing at {consent.get('answered_at')} UTC"
                out.append(row)
                continue
        tier = _tier_for(spec, c["kind"], max(0.0, notice))
        if tier is None:
            continue
        h, amount = pay_hours(tier.get("pay") or {}, c, rate)
        row.update(premium_hours=h, amount=amount if amount is not None else 0.0, rule=tier)
        if amount is None:
            row["status"] = "owed"
            row["exempt_reason"] = "no hourly rate on file — hours owed, dollars to be set"
        if covered is None:
            row["status"] = "unknown_coverage"
            row["exempt_reason"] = "covered only if their pay is under the law's ceiling — their rate isn't on file"
        out.append(row)
    return out


def rest_premium(spec_comp: dict, rate, gap_hours, need_hours, later_shift_hours):
    """(premium_hours or None, amount or None) a consented short rest owes:
    a flat sum per instance, the multiplier's extra on the whole later shift
    ("shift", Chicago) or on its hours inside the rest window."""
    comp = spec_comp or {}
    flat = comp.get("rest_premium_flat")
    if flat:
        return None, float(flat)
    mult = comp.get("rest_premium_multiplier")
    if not mult:
        return None, None
    scope = comp.get("rest_premium_scope") or "hours_inside"
    hours = float(later_shift_hours or 0) if scope == "shift" else max(0.0, min(float(need_hours) - float(gap_hours),
                                                                               float(later_shift_hours or 0)))
    extra = round(hours * (float(mult) - 1.0), 2)
    return extra, (round(extra * float(rate), 2) if rate else None)


# ── the restaurant's context ───────────────────────────────────────────────

def _context(restaurant_id, db_path=None, on=None):
    """(restaurant, code, entry, spec, rate_of, covered_of) — the law that
    holds the restaurant and how to price its people. (r, None, ...) when
    no predictive law covers it."""
    import compliance_packs as cp
    r = _models.get_restaurant(restaurant_id, db_path or DB_PATH)
    if not r:
        return None, None, None, None, None, None
    code, e = cp.predictive_entry(r, on)
    if not e:
        return r, None, None, None, None, None
    v = cp.value_on(e.get("predictability_pay"), on)
    spec = (v or {}).get("value") or None
    try:
        import labor as _lab
        people, typical = _lab.person_rate_book(restaurant_id)
    except Exception:
        people, typical = {}, {}
    roles = {}
    try:
        roles = {str(k).strip().lower(): float(v_) for k, v_ in (_models.get_role_rates(restaurant_id) or {}).items()
                 if k != "_default"}
    except Exception:
        roles = {}
    salaried = {}
    try:
        salaried = {_models.salaried_name_key(x["name"]): x.get("annual") for x in _models.salaried_staff(r)}
        for k in _models.salaried_keys(r, db_path):
            salaried.setdefault(k, None)
    except Exception:
        salaried = {}

    def rate_of(name, role):
        k = _key(name)
        rt = (people or {}).get(k) or roles.get((role or "").strip().lower()) or (typical or {}).get(
            (role or "").strip().lower())
        try:
            return float(rt) if rt else None
        except (TypeError, ValueError):
            return None

    def covered_of(name, rate):
        k = _key(name)
        return cp.employee_covered(r, rate=rate, annual=salaried.get(k), salaried=k in salaried, on=on, code=code)

    return r, code, e, spec, rate_of, covered_of


def _consent_lookup(restaurant_id, sent_at=None, db_path=None):
    import staff_consent

    def look(change):
        return staff_consent.consented_change(restaurant_id, change["employee"], change["date"],
                                              after=change.get("after"),
                                              before=None, db_path=db_path)
    return look


def preview(restaurant_id, before_rows, after_rows, now=None, db_path=None) -> dict:
    """The priced changes a Send would record, nothing written:
    {"covered": bool, "law", "code", "rows": [...], "total", "owed_count",
    "unknown_count", "text"} — `text` is the owner's one sentence."""
    from time_utils import restaurant_now_by_id
    now = now or restaurant_now_by_id(restaurant_id, naive=True)
    r, code, e, spec, rate_of, covered_of = _context(restaurant_id, db_path, on=now)
    if not spec:
        return {"covered": False, "law": None, "code": None, "rows": [], "total": 0.0, "owed_count": 0,
                "unknown_count": 0, "text": None}
    rows = price(classify(before_rows, after_rows, restaurant_id, db_path), spec, now, rate_of=rate_of, covered_of=covered_of,
                 consent_of=_consent_lookup(restaurant_id, db_path=db_path), code=code,
                 source=(e.get("predictability_pay") or [{}])[-1].get("source"), on=now)
    owed = [x for x in rows if x["status"] in ("owed", "unknown_coverage") and (x.get("premium_hours") or x.get("amount"))]
    total = round(sum(float(x.get("amount") or 0) for x in owed), 2)
    unknown = sum(1 for x in owed if x["status"] == "unknown_coverage" or x.get("rate") is None)
    text = None
    if owed:
        people = sorted({x["employee"] for x in owed})
        text = (f"{len(owed)} change{'s' if len(owed) != 1 else ''} inside {e['label']}'s "
                f"{int(spec.get('window_days') or 14)}-day notice window owe{'s' if len(owed) == 1 else ''} "
                f"predictability pay: ${total:,.2f} to {', '.join(people[:4])}{' and others' if len(people) > 4 else ''}"
                + (f" ({unknown} more to price — a rate or coverage isn't on file)" if unknown else "")
                + ". It's recorded for payroll when you send.")
    return {"covered": True, "law": e.get("law"), "code": code, "rows": rows, "total": total,
            "owed_count": len(owed), "unknown_count": unknown, "text": text}


def _insert(conn, restaurant_id, history_id, version, row, sent_at, sent_by):
    conn.execute(
        "INSERT INTO compliance_pay_ledger (restaurant_id, history_id, version, jurisdiction, person_id, employee_name, "
        "employee_key, shift_date, change_kind, before_json, after_json, minutes_changed, hours_lost, hours_added, "
        "notice_hours, initiator, exempt_reason, consent_id, premium_hours, rate, amount, rule_json, source, status, "
        "sent_at, sent_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (restaurant_id, history_id, version, row.get("jurisdiction"), row.get("person_id"), row["employee"],
         _key(row["employee"]), row.get("date") or "", row["kind"],
         json.dumps(row.get("before")) if row.get("before") else None,
         json.dumps(row.get("after")) if row.get("after") else None, row.get("minutes_changed"),
         row.get("hours_lost"), row.get("hours_added"), row.get("notice_hours"), row.get("initiator") or "employer",
         row.get("exempt_reason"), row.get("consent_id"), row.get("premium_hours"), row.get("rate"),
         float(row.get("amount") or 0), json.dumps(row.get("rule")) if row.get("rule") else None, row.get("source"),
         row.get("status") or "owed", sent_at.strftime("%Y-%m-%d %H:%M:%S") if isinstance(sent_at, datetime)
         else str(sent_at), (sent_by or "")[:120] or None))


def _last_published_rows(restaurant_id, history_id, db_path=None):
    from schedule_versions import rows_from_csv, materialize, materialize_one, VERSION_TEXT
    conn = get_conn(db_path)
    try:
        v = materialize_one(conn, conn.execute(
            "SELECT version, " + VERSION_TEXT + " FROM schedule_versions WHERE restaurant_id=? AND history_id=? "
            "AND reason='published' ORDER BY version DESC LIMIT 1", (restaurant_id, history_id)).fetchone(), history_id)
        swaps = materialize(conn, conn.execute(
            "SELECT version, " + VERSION_TEXT + " FROM schedule_versions WHERE restaurant_id=? AND history_id=? AND "
            "reason='swap' AND version > ? ORDER BY version",
            (restaurant_id, history_id, v["version"] if v else 0)).fetchall(), history_id)
    finally:
        conn.close()
    if not v:
        return None, []
    return rows_from_csv(v["schedule_csv"]), [rows_from_csv(s["schedule_csv"]) for s in swaps]


def record_send(restaurant_id, history_id, after_rows, before_rows=None, sent_by=None, now=None, version=None,
                db_path=None) -> dict:
    """Write the ledger rows for a Send of changes to a posted week: what
    staff were last told (the last `published` version, or `before_rows`)
    against what goes out now. A change a staff cover or swap made since
    (a `swap` version) was recorded when it happened and is left out here.
    Never raises into the send: a failure is captured and the send stands."""
    try:
        from time_utils import restaurant_now_by_id
        now = now or restaurant_now_by_id(restaurant_id, naive=True)
        r, code, e, spec, rate_of, covered_of = _context(restaurant_id, db_path, on=now)
        if not spec:
            return {"recorded": 0, "total": 0.0}
        swaps = []
        if before_rows is None:
            before_rows, swaps = _last_published_rows(restaurant_id, history_id, db_path)
            if before_rows is None:
                return {"recorded": 0, "total": 0.0}
        # changes the staff made themselves since (covers, swaps): already on
        # the record as employee-initiated
        staff_made = set()
        prev = before_rows
        for s in swaps:
            for c in classify(prev, s, restaurant_id, db_path):
                staff_made.add((c["key"], c["date"]))
            prev = s
        changes = [c for c in classify(before_rows, after_rows, restaurant_id, db_path) if (c["key"], c["date"]) not in staff_made]
        rows = price(changes, spec, now, rate_of=rate_of, covered_of=covered_of,
                     consent_of=_consent_lookup(restaurant_id, db_path=db_path), code=code,
                     source=(e.get("predictability_pay") or [{}])[-1].get("source"), on=now)
        conn = get_conn(db_path)
        try:
            for row in rows:
                _insert(conn, restaurant_id, history_id, version, row, now, sent_by)
            conn.commit()
        finally:
            conn.close()
        return {"recorded": len(rows), "total": round(sum(float(x.get("amount") or 0) for x in rows
                                                          if x["status"] in ("owed", "unknown_coverage")), 2)}
    except Exception as ex:
        try:
            import ops
            ops.capture(ex, job="predictability_pay_record", context=f"restaurant_id={restaurant_id} history={history_id}")
        except Exception:
            pass
        return {"recorded": 0, "total": 0.0, "error": True}


def record_request_change(restaurant_id, history_id, before_rows, after_rows, kind, actor=None, now=None,
                          offer_consent_id=None, db_path=None) -> dict:
    """A cover or swap the staff made (shift_requests), on the record. The
    person giving a shift up and both sides of a swap: employee-initiated,
    exempt. The person taking an EXTRA shift a manager posted (`kind`
    "extra"): hours the employer added, which the law prices unless their
    written acceptance (`offer_consent_id`, or the consent recorded with the
    claim) exempts it there. Never raises into the request."""
    try:
        from time_utils import restaurant_now_by_id
        now = now or restaurant_now_by_id(restaurant_id, naive=True)
        r, code, e, spec, rate_of, covered_of = _context(restaurant_id, db_path, on=now)
        if not spec:
            return {"recorded": 0}
        changes = classify(before_rows, after_rows, restaurant_id, db_path)
        src = (e.get("predictability_pay") or [{}])[-1].get("source")
        rows = []
        if kind == "extra":
            rows = price(changes, spec, now, rate_of=rate_of, covered_of=covered_of,
                         consent_of=_consent_lookup(restaurant_id, db_path=db_path), code=code, source=src, on=now)
        else:
            rows = price(changes, spec, now, rate_of=rate_of, covered_of=covered_of, initiator="employee",
                         code=code, source=src, on=now)
        conn = get_conn(db_path)
        try:
            for row in rows:
                _insert(conn, restaurant_id, history_id, None, row, now, actor)
            conn.commit()
        finally:
            conn.close()
        return {"recorded": len(rows)}
    except Exception as ex:
        try:
            import ops
            ops.capture(ex, job="predictability_pay_request", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return {"recorded": 0, "error": True}


def record_rest_premiums(restaurant_id, history_id, violations, rows, sent_by=None, now=None, db_path=None) -> dict:
    """The rest premium each consented short turnaround owes (the sweep's
    rest_premium flags), written when the week is sent. One row per
    instance; a second send of the same week does not record it twice."""
    try:
        from time_utils import restaurant_now_by_id
        import schedule_rules as _sr
        now = now or restaurant_now_by_id(restaurant_id, naive=True)
        r = _models.get_restaurant(restaurant_id, db_path or DB_PATH)
        comp = _sr.compliance(r)
        if _sr.rest_mode(comp) != "consent_premium":
            return {"recorded": 0}
        _r, code, e, spec, rate_of, covered_of = _context(restaurant_id, db_path, on=now)
        if violations is None:
            dates = sorted({x.get("date") for x in rows or [] if x.get("date")})
            if not dates:
                return {"recorded": 0}
            from datetime import datetime as _dtw
            c = _sr.build_constraints(restaurant_id, dates, [_dtw.strptime(d, "%Y-%m-%d").strftime("%A") for d in dates],
                                      restaurant=r, db_path=db_path or DB_PATH)
            violations = _sr.violations(rows, c)
        prem = [v for v in (violations or []) if v.get("kind") == "rest_premium"]
        if not prem:
            return {"recorded": 0}
        conn = get_conn(db_path)
        n = 0
        try:
            for v in prem:
                row = rows[v["index"]] if v.get("index") is not None and v["index"] < len(rows) else {}
                name = row.get("employee") or v.get("employee") or ""
                day = v.get("later_date") or row.get("date") or v.get("date") or ""
                dup = conn.execute("SELECT 1 FROM compliance_pay_ledger WHERE restaurant_id=? AND history_id=? AND "
                                   "employee_key=? AND shift_date=? AND change_kind='rest_premium'",
                                   (restaurant_id, history_id, _key(name), day)).fetchone()
                if dup:
                    continue
                rate = rate_of(name, row.get("role")) if rate_of else None
                need = float(comp.get("min_rest_hours") or 0)
                gap = max(0.0, need - float(v.get("premium_hours") or 0)) if comp.get("rest_premium_scope") != "shift" \
                    else 0.0
                h, amount = rest_premium(comp, rate, gap, need, _hours(row) if row else 0)
                rec = {"employee": name, "date": day, "kind": "rest_premium", "before": None, "after": _shift(row)
                       if row else None, "minutes_changed": None, "hours_lost": None, "hours_added": None,
                       "notice_hours": None, "initiator": "employee", "exempt_reason": None,
                       "consent_id": None, "premium_hours": h, "rate": rate, "amount": amount or 0.0,
                       "rule": {"rest_premium": {k: comp.get(k) for k in ("min_rest_hours", "rest_premium_multiplier",
                                                                          "rest_premium_flat", "rest_premium_scope")}},
                       "source": ((e or {}).get("rules") or {}).get("min_rest_hours", [{}])[-1].get("source")
                       if e else None, "jurisdiction": code,
                       "status": "owed" if amount is not None else "owed"}
                if amount is None:
                    rec["exempt_reason"] = "no hourly rate on file — hours owed, dollars to be set"
                _insert(conn, restaurant_id, history_id, None, rec, now, sent_by)
                n += 1
            conn.commit()
        finally:
            conn.close()
        return {"recorded": n}
    except Exception as ex:
        try:
            import ops
            ops.capture(ex, job="rest_premium_record", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return {"recorded": 0, "error": True}


# ── reading the ledger ─────────────────────────────────────────────────────

def ledger(restaurant_id, date_from=None, date_to=None, db_path=None, limit=5000) -> list:
    conn = get_conn(db_path)
    try:
        q = "SELECT * FROM compliance_pay_ledger WHERE restaurant_id=?"
        args = [restaurant_id]
        if date_from:
            q += " AND shift_date>=?"
            args.append(str(date_from)[:10])
        if date_to:
            q += " AND shift_date<=?"
            args.append(str(date_to)[:10])
        q += " ORDER BY shift_date, employee_key, id LIMIT ?"
        args.append(int(limit))
        out = []
        for r in conn.execute(q, args).fetchall():
            d = dict(r)
            for k in ("before_json", "after_json", "rule_json"):
                try:
                    d[k[:-5]] = json.loads(d.pop(k) or "null")
                except (TypeError, ValueError):
                    d[k[:-5]] = None
            out.append(d)
        return out
    finally:
        conn.close()


def summary(restaurant_id, date_from=None, date_to=None, db_path=None) -> dict:
    rows = ledger(restaurant_id, date_from, date_to, db_path)
    owed = [r for r in rows if r["status"] in ("owed", "unknown_coverage")]
    by_person = {}
    for r in owed:
        p = by_person.setdefault(r["employee_name"], {"employee": r["employee_name"], "amount": 0.0, "rows": 0,
                                                      "premium_hours": 0.0})
        p["amount"] = round(p["amount"] + float(r["amount"] or 0), 2)
        p["premium_hours"] = round(p["premium_hours"] + float(r["premium_hours"] or 0), 2)
        p["rows"] += 1
    return {"rows": len(rows), "owed": len(owed), "total": round(sum(float(r["amount"] or 0) for r in owed), 2),
            "exempt": sum(1 for r in rows if r["status"] == "exempt"),
            "by_person": sorted(by_person.values(), key=lambda p: -p["amount"])}


EXPORT_COLUMNS = ("shift_date", "employee_name", "change_kind", "before", "after", "notice_hours", "premium_hours",
                  "rate", "amount", "status", "exempt_reason", "initiator", "consent_id", "jurisdiction", "source",
                  "sent_at", "sent_by")


def export_csv(restaurant_id, date_from=None, date_to=None, db_path=None, mark_exported=False) -> str:
    """The ledger as payroll reads it: one line per changed shift, dates
    M/D/YY, before/after as times. `mark_exported` stamps the rows."""
    from time_utils import mdy
    rows = ledger(restaurant_id, date_from, date_to, db_path)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS)
    for r in rows:
        def shift(s):
            return f"{mdy(s.get('date'))} {s.get('start')}-{s.get('end')}" if s else ""
        w.writerow([mdy(r["shift_date"]), r["employee_name"], r["change_kind"], shift(r.get("before")),
                    shift(r.get("after")), r.get("notice_hours"), r.get("premium_hours"), r.get("rate"),
                    f"{float(r.get('amount') or 0):.2f}", r["status"], r.get("exempt_reason") or "",
                    r.get("initiator"), r.get("consent_id") or "", r.get("jurisdiction") or "", r.get("source") or "",
                    r.get("sent_at"), r.get("sent_by") or ""])
    if mark_exported and rows:
        conn = get_conn(db_path)
        try:
            conn.executemany("UPDATE compliance_pay_ledger SET exported_at=datetime('now') WHERE id=? AND "
                             "exported_at IS NULL", [(r["id"],) for r in rows])
            conn.commit()
        finally:
            conn.close()
    return buf.getvalue()


def cut_premium(restaurant_id, employee, role, shift_date, hours_cut, shift_hours, now=None, db_path=None) -> dict:
    """What letting someone go early today would owe under the law that
    covers the restaurant — the send-home suggestion nets it out of the
    saving. {"amount", "hours", "law"} or {"amount": 0} when nothing."""
    from time_utils import restaurant_now_by_id
    now = now or restaurant_now_by_id(restaurant_id, naive=True)
    r, code, e, spec, rate_of, covered_of = _context(restaurant_id, db_path, on=now)
    if not spec:
        return {"amount": 0.0, "hours": 0.0, "law": None}
    change = {"employee": employee, "key": _key(employee), "date": shift_date, "kind": "reduce",
              "before": None, "after": None, "minutes_changed": int(round(hours_cut * 60)),
              "start_shift_minutes": 0, "end_shift_minutes": int(round(hours_cut * 60)),
              "hours_lost": float(hours_cut), "hours_added": 0.0, "shift_hours": float(shift_hours or hours_cut),
              "first_start": now + timedelta(minutes=1)}
    rows = price([change], spec, now, rate_of=lambda n, rl: rate_of(employee, role), covered_of=covered_of,
                 code=code, on=now)
    if not rows or rows[0]["status"] not in ("owed", "unknown_coverage"):
        return {"amount": 0.0, "hours": 0.0, "law": e.get("law")}
    return {"amount": float(rows[0].get("amount") or 0), "hours": float(rows[0].get("premium_hours") or 0),
            "law": e.get("law"), "priced": rows[0].get("rate") is not None}
