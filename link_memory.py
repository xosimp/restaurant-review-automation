"""link_memory — what the cross-module links have been, kept (memory audit
9/29/26, "links").

business_intelligence.correlations() finds a link fresh on every call and
then forgot it: a link seen four weeks running was rediscovered each time
with no memory that it recurs, a link the owner acted on came back looking
new, and only one of the kinds reached a decision (the schedule draft read
reviews_x_labor, recomputing the whole cross-module read to get it).

Every link a read finds is observed here — at most one write per link per
day — into bi_links: one row per restaurant and link key (its kind and what
it is about), with the day it was first and last found, on how many days,
in how many ISO weeks running, and how it was resolved:

  done / implemented   the owner answered it (the ledger's Done, or the
                       change was made); found again RECUR_AFTER_DAYS later
                       it is reopened as still there after the answer
  not_for_us           the owner declined it: kept, and never re-surfaced
                       by a reader here (one "no" everywhere)
  gone                 a read that consulted both of its modules GONE_DAYS
                       after it was last found no longer found it (settle,
                       nightly); found again it is reopened as come back

A link found ESCALATE_WEEKS weeks running, or come back, is `recurring`:
the one-thing pick ranks it higher and says why (business_intelligence).

Readers (the consumers each link kind was missing):
  active(rid, kinds)       unresolved links found within ACTIVE_DAYS
  do_not_promote(rid)      dishes guests name in complaints that are also a
                           cost driver (reviews_x_menu): the Opportunity
                           Feed never proposes featuring one, and the
                           marketing generators read the list (link_lines)
  dish_guard(rid, dish)    the reprice guard: a dish guests are complaining
                           about is not repriced by one tap — fix the plate
                           before the price (menu_intelligence)
  link_lines(req)          memory_context provider "links": the food
                           diagnosis reads the food links, marketing reads
                           the do-not-promote list and its own links

Rows are small and bounded (one per restaurant and link subject) and kept
forever, like the rest of the learning record.
"""
import json
import logging
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH

log = logging.getLogger(__name__)

ESCALATE_WEEKS = 3          # found this many ISO weeks running: recurring
ACTIVE_DAYS = 14            # a reader only acts on a link found this recently
GONE_DAYS = 14              # not found in a read this long after it was last found: gone
RECUR_AFTER_DAYS = 28       # after Done, still found this much later: reopened
DECLINED = "not_for_us"
ANSWERED = ("done", "implemented")

# Which link kinds each memory_context surface reads.
SURFACE_KINDS = {
    "food_diagnosis": ("reviews_x_food_cost", "reviews_x_menu"),
    "marketing": ("reviews_x_menu", "marketing_x_reviews", "marketing_x_labor"),
}

# The view a link's line needs (memory_context's viewer check): the most
# guarded module it quotes — a food cost driver, a labor percentage.
LINE_MODULE = {"reviews_x_food_cost": "food", "reviews_x_menu": "food", "marketing_x_reviews": "marketing",
               "marketing_x_labor": "labor", "reviews_x_labor": "labor", "dsr_x_reviews": "labor",
               "intel_x_reviews": "intel"}

# A link names its modules; a read consulted a module when it read it.
_MODULE_DATA = {"reviews": "reviews", "labor": "labor", "food_cost": "food_cost", "marketing": "marketing",
                "intel": "visibility", "dsr": "dsr"}


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_link_memory(db_path: str = DB_PATH):
    """Boot DDL (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS bi_links (
            restaurant_id     INTEGER NOT NULL,
            link_key          TEXT    NOT NULL,
            kind              TEXT    NOT NULL,
            subject           TEXT,
            headline          TEXT,
            modules           TEXT,
            day               TEXT,
            dish              TEXT,
            detail            TEXT,
            first_seen        TEXT    NOT NULL,
            last_seen         TEXT    NOT NULL,
            times_seen        INTEGER NOT NULL DEFAULT 1,
            week_streak       INTEGER NOT NULL DEFAULT 1,
            last_week         TEXT,
            last_missed       TEXT,
            resolved_at       TEXT,
            resolved_by       TEXT,
            recurred_at       TEXT,
            recurred_after    TEXT,
            recurred_after_by TEXT,
            times_recurred    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (restaurant_id, link_key)
        )""")
        conn.commit()
    finally:
        conn.close()


def _today(restaurant_id):
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id).date()
    except Exception:
        return date.today()


def _day(v):
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _week(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _prev_week(d):
    return _week(d - timedelta(days=7))


def _mdy(v):
    from time_utils import mdy
    return mdy(v)


def _key(link):
    import business_intelligence as bi
    return bi.link_key(link)


def _detail(link):
    return json.dumps({k: link.get(k) for k in ("evidence", "confirm_by", "alternative", "not_a_cause", "category",
                                                "mentions") if link.get(k) is not None}, default=str)[:4000]


def consulted_modules(data) -> set:
    """The link modules a cross-module read consulted: a module's data was
    read (business_intelligence.gather). A link whose modules were not all
    consulted — a manager's view without Food Cost — was not looked for, so
    its absence says nothing."""
    data = data or {}
    out = {m for m, k in _MODULE_DATA.items() if data.get(k)}
    if not (data.get("labor") or {}).get("is_live"):
        out.discard("labor")          # sample shifts are not this restaurant's labor
    return out


def memory_of(row, today=None) -> dict:
    """A stored row as the `memory` a link carries: when it was found, how
    many weeks running, whether it came back, and the owner's words for it
    (M/D/YY)."""
    today = today or date.today()
    streak = int(row.get("week_streak") or 1)
    came_back = None
    if row.get("recurred_at") and row.get("recurred_after_by"):
        came_back = {"on": row["recurred_at"], "after": row["recurred_after_by"],
                     "resolved_on": row.get("recurred_after")}
    declined = row.get("resolved_by") == DECLINED
    if came_back and came_back["after"] in ANSWERED:
        label = (f"Still found after you marked it done on {_mdy(came_back['resolved_on'])}"
                 if came_back["after"] == "done" else
                 f"Still found after the change was made on {_mdy(came_back['resolved_on'])}")
    elif came_back:
        label = f"Back on {_mdy(came_back['on'])} after it went away"
    elif streak >= 2:
        label = f"Found {streak} weeks running, since {_mdy(row.get('first_seen'))}"
    else:
        label = f"First found {_mdy(row.get('first_seen'))}"
    return {"first_seen": row.get("first_seen"), "last_seen": row.get("last_seen"),
            "times_seen": int(row.get("times_seen") or 1), "weeks_running": streak,
            "recurring": bool(streak >= ESCALATE_WEEKS or came_back),
            "came_back": came_back, "declined": declined,
            "resolved": bool(row.get("resolved_at")), "label": label}


def observe(restaurant_id, links, consulted=None, today=None, db_path=None) -> list:
    """Store what a cross-module read found and annotate each link with its
    `memory` (memory_of). `consulted`: the modules the read consulted
    (consulted_modules) — an unresolved link of this restaurant NOT found by
    a read that consulted all its modules is stamped missed (settle turns an
    old miss into gone). Writes at most once per link per day. Returns the
    links. Never raises."""
    links = links or []
    if not restaurant_id:
        return links
    today = today or _today(restaurant_id)
    iso, wk, prev = today.isoformat(), _week(today), _prev_week(today)
    try:
        conn = get_conn(db_path)
    except Exception as e:
        log.warning("link_memory: no connection for rid=%s: %s", restaurant_id, e)
        return links
    try:
        rows = {r["link_key"]: dict(r) for r in conn.execute(
            "SELECT * FROM bi_links WHERE restaurant_id=?", (restaurant_id,)).fetchall()}
        found = set()
        for link in links:
            try:
                key = _key(link)
            except Exception:
                continue
            found.add(key)
            row = rows.get(key)
            if row is None:
                row = {"restaurant_id": restaurant_id, "link_key": key, "kind": link.get("kind") or "cross",
                       "subject": link.get("subject"), "headline": link.get("headline"),
                       "modules": json.dumps(link.get("modules") or []), "day": link.get("day"),
                       "dish": link.get("dish"), "detail": _detail(link), "first_seen": iso, "last_seen": iso,
                       "times_seen": 1, "week_streak": 1, "last_week": wk}
                conn.execute(
                    "INSERT OR IGNORE INTO bi_links (restaurant_id, link_key, kind, subject, headline, modules, day, "
                    "dish, detail, first_seen, last_seen, times_seen, week_streak, last_week) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (restaurant_id, key, row["kind"], row["subject"], row["headline"], row["modules"], row["day"],
                     row["dish"], row["detail"], iso, iso, 1, 1, wk))
                rows[key] = row
            elif str(row.get("last_seen") or "")[:10] < iso:
                last_week = row.get("last_week")
                streak = int(row.get("week_streak") or 1)
                streak = streak if last_week == wk else (streak + 1 if last_week == prev else 1)
                upd = {"headline": link.get("headline") or row.get("headline"), "detail": _detail(link),
                       "last_seen": iso, "times_seen": int(row.get("times_seen") or 0) + 1,
                       "week_streak": streak, "last_week": wk}
                by, at = row.get("resolved_by"), _day(row.get("resolved_at"))
                reopen = (by == "gone") or (by in ANSWERED and at is not None
                                            and (today - at).days >= RECUR_AFTER_DAYS)
                if reopen:
                    if by == "gone":
                        upd["week_streak"] = 1
                    upd.update({"resolved_at": None, "resolved_by": None, "recurred_at": iso,
                                "recurred_after": row.get("resolved_at"), "recurred_after_by": by,
                                "times_recurred": int(row.get("times_recurred") or 0) + 1})
                sets = ", ".join(f"{k}=?" for k in upd)
                conn.execute(f"UPDATE bi_links SET {sets} WHERE restaurant_id=? AND link_key=?",
                             (*upd.values(), restaurant_id, key))
                row.update(upd)
            link["memory"] = memory_of(row, today)
        if consulted is not None:
            have = set(consulted)
            for key, row in rows.items():
                if key in found or row.get("resolved_at"):
                    continue
                try:
                    mods = [m for m in json.loads(row.get("modules") or "[]") if m]
                except (TypeError, ValueError):
                    mods = []
                if mods and all(m in have for m in mods) and str(row.get("last_missed") or "") < iso:
                    conn.execute("UPDATE bi_links SET last_missed=? WHERE restaurant_id=? AND link_key=?",
                                 (iso, restaurant_id, key))
        conn.commit()
    except Exception as e:
        log.warning("link_memory: not observed for rid=%s: %s", restaurant_id, e)
    finally:
        conn.close()
    return links


def _answer(conn, restaurant_id, key, since):
    """The owner's answer to a link's recommendation since `since`:
    'done', 'implemented', 'not_for_us', or None, with its date."""
    ev = conn.execute(
        "SELECT event, at, meta FROM rec_events WHERE restaurant_id=? AND key=? AND at>=? AND event IN "
        "('completed','implemented','dismissed') ORDER BY at DESC, id DESC LIMIT 1",
        (restaurant_id, key, since)).fetchone()
    if ev is None:
        return None, None
    if ev["event"] == "dismissed":
        try:
            meta = json.loads(ev["meta"] or "{}") or {}
        except (TypeError, ValueError):
            meta = {}
        if meta.get("kind") != "not_for_us":
            return None, None
        return DECLINED, str(ev["at"])[:10]
    return ("done" if ev["event"] == "completed" else "implemented"), str(ev["at"])[:10]


def settle(restaurant_id, today=None, db_path=None) -> dict:
    """The nightly pass (learning_memory): resolve the links the owner
    answered, and the ones a later read no longer found (GONE_DAYS). Never
    raises. {"answered", "gone"}."""
    today = today or _today(restaurant_id)
    out = {"answered": 0, "gone": 0}
    try:
        conn = get_conn(db_path)
    except Exception:
        return out
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bi_links WHERE restaurant_id=? AND resolved_at IS NULL", (restaurant_id,)).fetchall()]
        for r in rows:
            since = r.get("recurred_at") or r.get("first_seen")
            by, at = _answer(conn, restaurant_id, r["link_key"], str(since)[:10])
            if by:
                conn.execute("UPDATE bi_links SET resolved_at=?, resolved_by=? WHERE restaurant_id=? AND link_key=?",
                             (at, by, restaurant_id, r["link_key"]))
                out["answered"] += 1
                continue
            last, missed = _day(r.get("last_seen")), _day(r.get("last_missed"))
            if last and missed and (today - last).days >= GONE_DAYS and (missed - last).days >= GONE_DAYS:
                conn.execute("UPDATE bi_links SET resolved_at=?, resolved_by='gone' WHERE restaurant_id=? "
                             "AND link_key=?", (today.isoformat(), restaurant_id, r["link_key"]))
                out["gone"] += 1
        conn.commit()
    except Exception as e:
        log.warning("link_memory: not settled for rid=%s: %s", restaurant_id, e)
    finally:
        conn.close()
    return out


def active(restaurant_id, kinds=None, within_days=ACTIVE_DAYS, today=None, db_path=None) -> list:
    """Links found within `within_days` and not resolved (a declined link is
    resolved), newest first, each with its `memory`. Never raises."""
    if not restaurant_id:
        return []
    today = today or _today(restaurant_id)
    since = (today - timedelta(days=within_days)).isoformat()
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    try:
        args = [restaurant_id, since]
        extra = ""
        if kinds:
            extra = f" AND kind IN ({','.join('?' for _ in kinds)})"
            args += list(kinds)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bi_links WHERE restaurant_id=? AND last_seen>=? AND resolved_at IS NULL" + extra
            + " ORDER BY week_streak DESC, last_seen DESC", args).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()
    for r in rows:
        try:
            r["detail"] = json.loads(r.get("detail") or "{}") or {}
        except (TypeError, ValueError):
            r["detail"] = {}
        r["memory"] = memory_of(r, today)
    return rows


def history(restaurant_id, db_path=None) -> list:
    """Every link kept for this restaurant, resolved ones included, newest
    first (Ask, the admin console). Never raises."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bi_links WHERE restaurant_id=? ORDER BY last_seen DESC", (restaurant_id,)).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()
    for r in rows:
        r["memory"] = memory_of(r)
    return rows


# ── the consumers ────────────────────────────────────────────────────────────

def _complaints_phrase(row):
    d = row.get("detail") or {}
    n, cat = d.get("mentions"), d.get("category")
    if n and cat:
        try:
            from analyser import category_label
            cat = category_label(cat)
        except Exception:
            cat = str(cat).replace("_", " ")
        return f"{n} {cat} complaints"
    return "complaints"


def do_not_promote(restaurant_id, db_path=None) -> dict:
    """{dish as guests name it: {"dish", "reason", "link_key", "since"}} —
    the dishes a live reviews_x_menu link names: guests name them in
    complaints and Food Cost ranks them as a cost driver. Marketing never
    puts one in front of guests while the link stands. Never raises."""
    out = {}
    for r in active(restaurant_id, kinds=("reviews_x_menu",), db_path=db_path):
        dish = str(r.get("dish") or "").strip()
        if not dish:
            continue
        complaints = _complaints_phrase(r)
        out[dish] = {"dish": dish, "link_key": r["link_key"], "since": r.get("first_seen"), "complaints": complaints,
                     "reason": f"guests name it in {complaints} and it is a food cost driver"}
    return out


def names_dish(listed, name) -> dict | None:
    """The do_not_promote / guard entry that plainly refers to the menu's
    `name` (business_intelligence._same_thing: a shared significant word),
    or None."""
    if not listed or not name:
        return None
    try:
        from business_intelligence import _same_thing
    except Exception:
        return None
    for dish, entry in listed.items():
        if _same_thing(dish, name):
            return entry
    return None


def dish_guard(restaurant_id, dish, listed=None, db_path=None) -> dict | None:
    """The reprice guard for one dish: {"kind", "text", "link_key", "since"}
    when guests are naming it in complaints (a live reviews_x_menu link), so
    the price is not raised by one tap before the plate is looked at
    (menu_intelligence._verdict's own rule: fix the plate before repricing
    it). `listed`: do_not_promote's result, read once for many dishes."""
    entry = names_dish(listed if listed is not None else do_not_promote(restaurant_id, db_path=db_path), dish)
    if not entry:
        return None
    return {"kind": "guest_complaints", "link_key": entry["link_key"], "since": entry.get("since"),
            "text": (f"Guests are naming {entry['dish']} in {entry.get('complaints') or 'complaints'} — look at the "
                     f"plate before raising its price.")}


def link_lines(req) -> list:
    """memory_context provider "links": the cross-module links this
    surface acts on (SURFACE_KINDS), each with how long it has stood. The
    food diagnosis reads the food links as context — co-occurrences, never
    a cause; marketing reads the do-not-promote list and its own links.
    Lines carry guest-derived words (a dish as guests named it), so none is
    trusted: the assembler fences them."""
    kinds = SURFACE_KINDS.get(getattr(req, "surface", None))
    if not kinds:
        return []
    rows = active(req.restaurant_id, kinds=kinds, db_path=getattr(req, "db_path", None))
    lines = []
    for i, r in enumerate(rows[:6]):
        measured = None
        m = r["memory"]
        when = m["label"][:1].lower() + m["label"][1:]
        module = LINE_MODULE.get(r["kind"], "food")
        if r["kind"] == "reviews_x_menu" and req.surface == "marketing" and r.get("dish"):
            # What a post needs, and no more: the marketing drafts are read by
            # logins without food-cost view (memory_context reads the marketing
            # surface as the team), so the line carries the instruction and the
            # guests' side of why — never the dish's cost (INT #41).
            # The dish is guests' words (fenced); the instruction is Cavnar
            # AI's own, so it is the line's trusted suffix — inside the guest
            # fence it read as a stranger's text never to follow (memory
            # re-audit 9/29/26, PROMPTS-1).
            text = f"A dish guests keep complaining about: {r['dish']}"
            measured = (f"DO NOT PROMOTE that dish: guests name it in {_complaints_phrase(r)} ({when}). Do not "
                        f"feature, discount or push it until that clears.")
            module = "reviews"
        else:
            d = r.get("detail") or {}
            text = f"{r.get('headline')} ({when}) — two modules pointing at the same thing, not a proven cause"
            if d.get("confirm_by"):
                text += f". Confirm by: {d['confirm_by']}"
        line = {"text": text, "date": r.get("last_seen"), "source": "link", "subject": r["link_key"],
                "weight": 10.0 - i + (2.0 if m["recurring"] else 0.0), "trusted": False,
                "module": module}
        if measured:
            line["measured"] = measured
        lines.append(line)
    return lines
