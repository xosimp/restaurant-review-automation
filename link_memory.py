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
# A link that ended on its own terms (re-audit 9/29/26, CROSSMODULE-12): a
# fill campaign's night has passed and was measured, or passed with nothing
# measured. Found again (a new campaign for the same night) it reopens.
ENDED = ("measured", "window_closed")

# Which link kinds each memory_context surface reads.
SURFACE_KINDS = {
    "food_diagnosis": ("reviews_x_food_cost", "reviews_x_menu"),
    "marketing": ("reviews_x_menu", "marketing_x_reviews", "marketing_x_labor"),
    # The review diagnosis reads the links that join its complaints to a
    # staffing fact or the rating (re-audit 9/29/26, CROSSMODULE-9).
    "review_diagnosis": ("reviews_x_labor", "dsr_x_reviews", "intel_x_reviews"),
    # The schedule reads what a fill campaign's night measured, once the
    # link has ended (CROSSMODULE-12): the verdict for labor:day:<day>.
    "schedule": ("marketing_x_labor",),
}
# Surfaces that read a kind's ENDED rows (measured / window closed) as well
# as its live ones, and for how long after the end.
ENDED_SURFACE_KINDS = {"schedule": ("marketing_x_labor",)}
ENDED_READ_DAYS = 120

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
            menu_item_id      INTEGER,
            PRIMARY KEY (restaurant_id, link_key)
        )""")
        # The one menu row a reviews_x_menu link is about (re-audit 9/29/26,
        # CROSSMODULE-14): "the chicken" blocked every chicken dish.
        have = {r[1] for r in conn.execute("PRAGMA table_info(bi_links)").fetchall()}
        if "menu_item_id" not in have:
            conn.execute("ALTER TABLE bi_links ADD COLUMN menu_item_id INTEGER")
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
    read (business_intelligence.gather) AND is current by the module's own
    flag. A link whose modules were not all consulted — a manager's view
    without Food Cost, a restaurant whose waste logging lapsed — was not
    looked for, so its absence says nothing: a measurement that stopped is
    never recorded as a finding that resolved (re-audit 9/29/26,
    CROSSMODULE-8)."""
    data = data or {}
    out = {m for m, k in _MODULE_DATA.items() if data.get(k)}
    if not (data.get("labor") or {}).get("is_live"):
        out.discard("labor")          # sample shifts are not this restaurant's labor
    food = data.get("food_cost") or {}
    if not (food.get("weekday_waste") or {}).get("has_data"):
        out.discard("food_cost")      # no waste logged in the window: nothing current to find
    rev = data.get("reviews") or {}
    if not int((((rev.get("brief") or {}).get("coverage") or {}).get("total")) or 0) and not rev.get("clusters"):
        out.discard("reviews")        # no reviews on file
    if (data.get("visibility") or {}).get("stale"):
        out.discard("intel")          # an out-of-date visibility run looked for nothing
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
    elif came_back and came_back["after"] in ENDED:
        label = f"Back on {_mdy(came_back['on'])} with a new campaign"
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
                       "times_seen": 1, "week_streak": 1, "last_week": wk,
                       "menu_item_id": link.get("menu_item_id")}
                conn.execute(
                    "INSERT OR IGNORE INTO bi_links (restaurant_id, link_key, kind, subject, headline, modules, day, "
                    "dish, detail, first_seen, last_seen, times_seen, week_streak, last_week, menu_item_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (restaurant_id, key, row["kind"], row["subject"], row["headline"], row["modules"], row["day"],
                     row["dish"], row["detail"], iso, iso, 1, 1, wk, row["menu_item_id"]))
                rows[key] = row
            elif str(row.get("last_seen") or "")[:10] < iso or row.get("resolved_by") in ENDED:
                # (an ENDED link found again — a new campaign for the same
                # night — reopens the same day it ended)
                last_week = row.get("last_week")
                streak = int(row.get("week_streak") or 1)
                streak = streak if last_week == wk else (streak + 1 if last_week == prev else 1)
                by, at = row.get("resolved_by"), _day(row.get("resolved_at"))
                if by == DECLINED:
                    # A declined link's weeks do not run on (CROSSMODULE-3):
                    # its streak stays where the owner answered it.
                    streak = int(row.get("week_streak") or 1)
                upd = {"headline": link.get("headline") or row.get("headline"), "detail": _detail(link),
                       "last_seen": iso, "times_seen": int(row.get("times_seen") or 0) + 1,
                       "week_streak": streak, "last_week": wk}
                if link.get("menu_item_id") is not None:
                    upd["menu_item_id"] = link["menu_item_id"]
                reopen = (by == "gone") or (by in ENDED) or (by in ANSWERED and at is not None
                                                            and (today - at).days >= RECUR_AFTER_DAYS)
                if reopen:
                    if by == "gone" or by in ENDED:
                        upd["week_streak"] = 1
                    upd.update({"resolved_at": None, "resolved_by": None, "recurred_at": iso,
                                "recurred_after": row.get("resolved_at"), "recurred_after_by": by,
                                "times_recurred": int(row.get("times_recurred") or 0) + 1})
                    if by in ANSWERED:
                        # "Still found after you marked it done" reaches the
                        # surfaces only if the ledger's Done lets it
                        # (CROSSMODULE-4): a Done on a link held its key
                        # silent for DONE_DAYS, so the reopening was never
                        # seen. A decline is never lifted.
                        _lift_answer(conn, restaurant_id, key)
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


def _local_day(restaurant_id, stamp):
    """A UTC event stamp's restaurant-local day (ISO). rec_events.at is UTC,
    and its date part is tomorrow every evening in the Americas — which
    pushed a link's resolved day, and so its reopening, a day late."""
    if not stamp:
        return None
    try:
        from time_utils import parse_stamp, restaurant_now_by_id
        at = parse_stamp(stamp)
        tz = restaurant_now_by_id(restaurant_id).tzinfo
        if at and tz:
            return at.astimezone(tz).date().isoformat()
    except Exception:
        pass
    return str(stamp)[:10]


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
        return DECLINED, _local_day(restaurant_id, ev["at"])
    return ("done" if ev["event"] == "completed" else "implemented"), _local_day(restaurant_id, ev["at"])


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


def _lift_answer(conn, restaurant_id, key):
    """End the ledger silence a Done (or the change made) put on a link's
    key, on the caller's connection — never a decline's. Never raises."""
    try:
        import rec_ledger
        from datetime import datetime as _dt
        now = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        for r in conn.execute("SELECT rec_id FROM rec_instances WHERE restaurant_id=? AND key=? "
                              "AND status IN ('completed','implemented','accepted') AND silenced_until IS NOT NULL "
                              "AND silenced_until > ?", (restaurant_id, key, now)).fetchall():
            rec_ledger.lift_silence(conn, r["rec_id"], "link_recurred")
    except Exception as e:
        log.warning("link_memory: done silence not lifted for rid=%s %s: %s", restaurant_id, key, e)


def end(restaurant_id, key, by, headline=None, detail=None, today=None, db_path=None) -> bool:
    """End an unresolved link on its own terms (ENDED: "measured" — its
    night was measured — or "window_closed"), keeping what it ended with:
    the measured sentence becomes its headline and joins its detail
    (CROSSMODULE-12). A link never observed is not written. Never
    raises."""
    if by not in ENDED or not restaurant_id or not key:
        return False
    today = today or _today(restaurant_id)
    try:
        conn = get_conn(db_path)
    except Exception:
        return False
    try:
        row = conn.execute("SELECT detail FROM bi_links WHERE restaurant_id=? AND link_key=? AND resolved_at IS NULL",
                           (restaurant_id, key)).fetchone()
        if row is None:
            return False
        try:
            d = json.loads(row["detail"] or "{}") or {}
        except (TypeError, ValueError):
            d = {}
        d.update(detail or {})
        sets, args = ["resolved_at=?", "resolved_by=?", "detail=?"], [today.isoformat(), by,
                                                                      json.dumps(d, default=str)[:4000]]
        if headline:
            sets.append("headline=?")
            args.append(str(headline)[:400])
        conn.execute(f"UPDATE bi_links SET {', '.join(sets)} WHERE restaurant_id=? AND link_key=?",
                     (*args, restaurant_id, key))
        conn.commit()
        return True
    except Exception as e:
        log.warning("link_memory: not ended for rid=%s %s: %s", restaurant_id, key, e)
        return False
    finally:
        conn.close()


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


def ended(restaurant_id, kinds=None, within_days=ENDED_READ_DAYS, today=None, db_path=None) -> list:
    """Links that ENDED on their own terms (ENDED) within `within_days`,
    newest first, each with its `memory` and parsed `detail`. Never
    raises."""
    if not restaurant_id:
        return []
    today = today or _today(restaurant_id)
    since = (today - timedelta(days=within_days)).isoformat()
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    try:
        args = [restaurant_id, since, *ENDED]
        extra = ""
        if kinds:
            extra = f" AND kind IN ({','.join('?' for _ in kinds)})"
            args += list(kinds)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bi_links WHERE restaurant_id=? AND resolved_at>=? AND resolved_by IN (?,?)" + extra
            + " ORDER BY resolved_at DESC", args).fetchall()]
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


_STATUS_WORDS = {DECLINED: "you passed on it", "done": "you marked it done", "implemented": "the change was made",
                 "gone": "no longer found", "measured": "its night was measured",
                 "window_closed": "its night passed with nothing measured"}


def history_lines(restaurant_id, denied=(), limit=12, db_path=None) -> list:
    """The link history Ask reads (read_restaurant_memory, re-audit 9/29/26
    CROSSMODULE-15): every kept link, open or resolved, newest first —
    {kind, headline, status, resolved_on, first_found, last_found, label,
    times_came_back} — so "have we seen this Friday problem before, and did
    I pass on it?" has an answer. `denied`: the login's denied module views
    (ask_cavnar_tools._denied — "inventory", "labor", …); a link whose line
    needs one of them (LINE_MODULE) is left out. Dates M/D/YY. Never
    raises."""
    denied = set(denied or ())
    out = []
    for r in history(restaurant_id, db_path=db_path):
        module = LINE_MODULE.get(r.get("kind"), "food")
        if {"food": "inventory"}.get(module, module) in denied:
            continue
        by = r.get("resolved_by")
        out.append({"kind": r.get("kind"), "headline": r.get("headline"),
                    "status": _STATUS_WORDS.get(by, "open") if by else "open",
                    "resolved_on": _mdy(r.get("resolved_at")) if r.get("resolved_at") else None,
                    "first_found": _mdy(r.get("first_seen")), "last_found": _mdy(r.get("last_seen")),
                    "label": (r.get("memory") or {}).get("label"),
                    "times_came_back": int(r.get("times_recurred") or 0)})
        if len(out) >= limit:
            break
    return out


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
        mid, menu_name = r.get("menu_item_id"), None
        if mid is None:
            # A link stored before its dish was resolved to one menu row.
            mid, menu_name = resolve_menu_item(restaurant_id, dish, db_path=db_path)
        else:
            menu_name = _menu_name(restaurant_id, mid, db_path=db_path)
        out[dish] = {"dish": dish, "link_key": r["link_key"], "since": r.get("first_seen"), "complaints": complaints,
                     "menu_item_id": mid, "menu_item": menu_name,
                     "reason": f"guests name it in {complaints} and it is a food cost driver"}
    return out


# ── the one menu row a complaint is about (re-audit 9/29/26, CROSSMODULE-14) ─
#
# The link's dish is the review analyser's entity — "the chicken" — and
# matching it on any shared word blocked Chicken Parm, the Chicken Caesar
# and the Buffalo Chicken Wings from marketing and put a reprice guard on
# each. The link now names ONE menu row: the driver's own dish when Food
# Cost's driver is a menu item, else among the menu items the guests' words
# name (a shared significant word, business_intelligence._same_thing) the
# one that uses the driver's ingredient, then the best seller over the last
# MENU_SALES_DAYS. The guard and the do-not-promote list match on that id.
MENU_SALES_DAYS = 28


def _norm_name(v):
    import re
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", str(v or "").lower()).split())


def _menu_name(restaurant_id, menu_item_id, db_path=None):
    try:
        conn = get_conn(db_path)
        try:
            r = conn.execute("SELECT name FROM menu_items WHERE id=? AND restaurant_id=?",
                             (menu_item_id, restaurant_id)).fetchone()
        finally:
            conn.close()
        return r["name"] if r else None
    except Exception:
        return None


def resolve_menu_item(restaurant_id, dish, driver_item=None, driver_kind=None, today=None, db_path=None) -> tuple:
    """(menu_item_id, name) of the ONE active menu row a complaint's `dish`
    is about, or (None, None). `driver_item` / `driver_kind`: Food Cost's
    driver the link joined (a "menu" driver names the dish itself; any other
    names an ingredient the dish should use). Never raises."""
    try:
        from business_intelligence import _same_thing
        today = today or _today(restaurant_id)
        conn = get_conn(db_path)
        try:
            menu = [dict(r) for r in conn.execute(
                "SELECT id, name FROM menu_items WHERE restaurant_id=? AND COALESCE(is_active,1)=1",
                (restaurant_id,)).fetchall()]
            if driver_item and driver_kind == "menu":
                hit = next((m for m in menu if _norm_name(m["name"]) == _norm_name(driver_item)), None)
                if hit:
                    return hit["id"], hit["name"]
            cands = [m for m in menu if _same_thing(dish, m["name"])]
            if not cands:
                return None, None
            ids = [m["id"] for m in cands]
            marks = ",".join("?" for _ in ids)
            sold = {}
            try:
                sold = {r["menu_item_id"]: float(r["q"] or 0) for r in conn.execute(
                    f"SELECT menu_item_id, SUM(qty_sold) AS q FROM menu_item_sales WHERE restaurant_id=? "
                    f"AND business_date >= ? AND menu_item_id IN ({marks}) GROUP BY menu_item_id",
                    (restaurant_id, (today - timedelta(days=MENU_SALES_DAYS)).isoformat(), *ids)).fetchall()}
            except Exception:
                sold = {}
            uses = set()
            if driver_item and driver_kind != "menu":
                try:
                    uses = {r["menu_item_id"] for r in conn.execute(
                        f"SELECT ri.menu_item_id FROM recipe_ingredients ri JOIN ingredients g ON g.id=ri.ingredient_id "
                        f"WHERE ri.menu_item_id IN ({marks}) AND lower(g.name)=?",
                        (*ids, str(driver_item).strip().lower())).fetchall()}
                except Exception:
                    uses = set()
        finally:
            conn.close()
        best = sorted(cands, key=lambda m: (m["id"] not in uses, -sold.get(m["id"], 0.0),
                                            len(_norm_name(m["name"]).split()), m["id"]))[0]
        return best["id"], best["name"]
    except Exception as e:
        log.warning("link_memory: dish not resolved for rid=%s: %s", restaurant_id, e)
        return None, None


def names_dish(listed, name, menu_item_id=None) -> dict | None:
    """The do_not_promote / guard entry about the menu dish `name` (its
    menu_items id when the caller has it), or None. An entry resolved to
    one menu row matches that row only — by id, else by its exact name
    (CROSSMODULE-14); an entry no menu row matched falls back to a shared
    significant word (business_intelligence._same_thing)."""
    if not listed or not (name or menu_item_id is not None):
        return None
    try:
        from business_intelligence import _same_thing
    except Exception:
        return None
    for dish, entry in listed.items():
        mid = entry.get("menu_item_id")
        if mid is not None:
            if menu_item_id is not None:
                if int(menu_item_id) == int(mid):
                    return entry
                continue
            if entry.get("menu_item") and _norm_name(entry["menu_item"]) == _norm_name(name):
                return entry
            continue
        if name and _same_thing(dish, name):
            return entry
    return None


def dish_guard(restaurant_id, dish, listed=None, db_path=None, menu_item_id=None) -> dict | None:
    """The reprice guard for one dish: {"kind", "text", "link_key", "since"}
    when guests are naming it in complaints (a live reviews_x_menu link), so
    the price is not raised by one tap before the plate is looked at
    (menu_intelligence._verdict's own rule: fix the plate before repricing
    it). `listed`: do_not_promote's result, read once for many dishes."""
    entry = names_dish(listed if listed is not None else do_not_promote(restaurant_id, db_path=db_path), dish,
                       menu_item_id=menu_item_id)
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
    surface = getattr(req, "surface", None)
    kinds = SURFACE_KINDS.get(surface)
    if not kinds:
        return []
    live_kinds = tuple(k for k in kinds if k not in ENDED_SURFACE_KINDS.get(surface, ()))
    rows = active(req.restaurant_id, kinds=live_kinds, db_path=getattr(req, "db_path", None)) if live_kinds else []
    lines = []
    # What a campaign's night measured, once its link ended (CROSSMODULE-12):
    # the verdict the schedule reads for that weekday — measured here, before
    # and after, never proof.
    for r in ended(req.restaurant_id, kinds=ENDED_SURFACE_KINDS.get(surface), db_path=getattr(req, "db_path", None)
                   )[:4] if ENDED_SURFACE_KINDS.get(surface) else []:
        d = r.get("detail") or {}
        text = d.get("outcome") or r.get("headline")
        if not text:
            continue
        lines.append({"text": text, "date": r.get("resolved_at"), "source": "link",
                      "subject": f"labor:day:{str(r.get('day') or '').lower()}" if r.get("day") else r["link_key"],
                      "weight": 8.0, "trusted": True, "module": LINE_MODULE.get(r["kind"], "labor")})
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
