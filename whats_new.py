"""
whats_new.py — the "What's new" list under the account menu (web) and its
iOS twin: what Cavnar AI shipped that an owner can see, newest first.

The entries live HERE, in the repo, and `sync()` writes any that are missing
to `changelog_entries` at boot, so a release note ships with the push that
ships the feature. The admin console's changelog editor still adds and
deletes rows by hand; a row it deleted that is still listed here comes back
on the next boot, so retire an entry by removing it from ENTRIES and the row.

Seen is per login (`changelog_seen`), not per restaurant: Danny opening the
list must not clear Erik's dot, and an admin's view-as never marks it read.
A login with no row yet falls back to the restaurant's old
`changelog_seen_at` stamp, so nothing reads as new twice.

Every stamp is 'YYYY-MM-DD HH:MM:SS' (SQLite's datetime('now') form): the
old seen stamp had a 'T', and ' ' sorts before 'T', so an entry published
later on the day the list was last opened never read as new.

Owner-facing words only: what changed for the restaurant, never how it was
built. Nothing for a surface that is not live (the staff app until it is in
the App Store).
"""
from datetime import datetime, timezone

import models as _models


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path else _models.get_conn()


# (date, tag, title, body) — tag is feature | improvement | fix.
ENTRIES = [
    ("2026-09-27", "feature", "Schedule Studio",
     "A full-page scheduling workspace: Cavnar AI drafts the week from your sales and your team's history, "
     "scores it, and you edit, redo a day or publish from one screen."),
    ("2026-09-27", "improvement", "New notifications and messages",
     "The bell and the messages panel were rebuilt: one clear row per alert, the action on the row, "
     "and conversations with your team beside them."),
    ("2026-09-28", "feature", "Campaign Studio",
     "Pick one goal and Cavnar AI drafts the text, the email and the social post for it together, "
     "in your restaurant's voice. Under Marketing."),
    ("2026-09-28", "feature", "RPOWER connected",
     "Sales, labor, comps and voids come straight from RPOWER every night, no exports to upload."),
    ("2026-09-30", "feature", "Reports tab",
     "Last night's Daily Sales Report every morning: net sales, labor, comps and voids, "
     "with a figure strip for each night."),
    ("2026-09-30", "improvement", "Labor % includes salaries",
     "The owner's labor % now counts salaried pay as well as hourly shifts, and your target judges that "
     "all-in figure. The hourly-only figure sits beside it."),
    ("2026-09-30", "feature", "Passkeys and Sign in with Apple",
     "Sign in to the web app with Face ID or Touch ID, or with your Apple account."),
    ("2026-10-01", "feature", "Game nights",
     "Follow the Bears, Bulls, Blackhawks, Cubs, White Sox or Fire. The morning brief names a game up to "
     "three days out, with what nights like it did here and a staffing plan once two have been measured."),
    ("2026-10-08", "feature", "Restaurant DNA",
     "A profile of your restaurant earned from its own nights: when it's busy, what sells, how it's staffed. "
     "On Home, with the full picture a tap away."),
    ("2026-10-10", "improvement", "Schedules learn your usual crew",
     "Drafts start from who usually works each role and shift in your POS history, and follow recent "
     "trends, like a night that now needs more servers than it used to."),
    ("2026-10-12", "feature", "Search phrases in review replies",
     "Add the phrases you want Google to connect with you, and drafted replies work one in where it reads "
     "naturally. Under Reviews, next to Analytics."),
    ("2026-10-12", "improvement", "Most recent and This month on Reviews",
     "Two new filters on Reviews: every review newest first, or only this month's."),
    ("2026-10-12", "improvement", "What each Home number means",
     "Hover a figure at the top of Home to see what it counts: your all-time Google rating beside the "
     "last 30 days, labor with and without salaries, and more."),
    ("2026-10-12", "fix", "One labor alert per pattern",
     "A manager's comp or void pattern is now one alert that updates each night, instead of a new copy "
     "every morning."),
]

_STAMP = "%Y-%m-%d %H:%M:%S"


def _now():
    return datetime.now(timezone.utc).strftime(_STAMP)


def _norm(stamp):
    """Any stored stamp in the one comparable form ('T' -> ' ', no zone)."""
    if not stamp:
        return None
    return str(stamp).replace("T", " ")[:19]


def sync(db_path=None) -> int:
    """Insert every ENTRIES row not already in changelog_entries (matched on
    title), published at noon UTC on its date. Boot only; returns how many
    were added. Never raises: a release note must not stop a boot."""
    try:
        conn = get_conn(db_path)
        try:
            have = {r[0] for r in conn.execute("SELECT title FROM changelog_entries").fetchall()}
            added = 0
            for day, tag, title, body in ENTRIES:
                if title in have:
                    continue
                conn.execute("INSERT INTO changelog_entries (title, body, tag, published_at, is_published) "
                             "VALUES (?,?,?,?,1)", (title, body, tag, f"{day} 12:00:00"))
                added += 1
            conn.commit()
            return added
        finally:
            conn.close()
    except Exception as e:
        print(f"[whats_new] sync skipped: {e}")
        return 0


def seen_at(current_user, db_path=None):
    """This login's last open of the list, else the restaurant's old stamp."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT seen_at FROM changelog_seen WHERE user_id=?",
                           (current_user.get("id"),)).fetchone()
    finally:
        conn.close()
    if row and row[0]:
        return _norm(row[0])
    r = _models.get_restaurant(current_user["restaurant_id"])
    return _norm(getattr(r, "changelog_seen_at", None)) if r else None


def _entries(since=None, db_path=None):
    conn = get_conn(db_path)
    try:
        if since:
            rows = conn.execute("SELECT * FROM changelog_entries WHERE is_published=1 AND "
                                "REPLACE(published_at, 'T', ' ') > ? ORDER BY published_at DESC, id DESC",
                                (since,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM changelog_entries WHERE is_published=1 "
                                "ORDER BY published_at DESC, id DESC LIMIT 30").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def unread_count(current_user, db_path=None) -> int:
    since = seen_at(current_user, db_path)
    return len(_entries(since, db_path)) if since else len(_entries(None, db_path))


def open_list(current_user, db_path=None):
    """The list, each entry flagged `is_new` against this login's last open,
    then that login marked as having seen it — unless an admin is viewing
    as the owner (permissions.acting_via), which reads without marking."""
    since = seen_at(current_user, db_path)
    entries = _entries(None, db_path)
    for e in entries:
        e["is_new"] = bool(since is None or (_norm(e.get("published_at")) or "") > since)
    try:
        from permissions import acting_via
        viewing_as = bool(acting_via(current_user)) or (current_user.get("device_type") or "") == "admin-view-as"
    except Exception:
        viewing_as = False
    if not viewing_as and current_user.get("id"):
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO changelog_seen (user_id, seen_at) VALUES (?, ?) "
                         "ON CONFLICT(user_id) DO UPDATE SET seen_at=excluded.seen_at",
                         (current_user["id"], _now()))
            conn.commit()
        finally:
            conn.close()
    return entries
