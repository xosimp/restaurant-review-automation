"""marketing_drafts.py — saved copy, and the approval step in front of publishing.

Generated content survived exactly as long as the screen it was on. Write a
caption, get called to the floor, come back — gone. There was no saved copy,
no "hold this for Tuesday", and no way for the person who writes the post to
be different from the person who decides it goes out, which is how most
restaurants above one location actually work.

Approval is scoped to the restaurant's PRIMARY login. An invited teammate can
write and save all day; releasing it belongs to the account that invited them.

The role vocabulary here is the one auth.py already established, and getting
it wrong has bitten this codebase before: 'client' is every restaurant's
primary login and the default, 'owner' is Will's multi-restaurant login, and
'member' is an invited teammate (see invite_team_member's comment — the Team
feature's first cut gated on role == 'owner', which no real client login has,
so it 403'd for every account). The gate is therefore "not a member", never
"is an owner".
"""
import logging

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)

MAX_BODY = 6000


def save_draft(restaurant_id, body, *, content_type=None, topic=None, media_id=None,
               draft_id=None, user_id=None, db_path: str = DB_PATH) -> dict:
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "There's nothing to save."}
    if len(body) > MAX_BODY:
        return {"ok": False, "error": "That's longer than any platform will take."}

    conn = get_conn(db_path)
    try:
        if draft_id:
            # Editing an approved draft sends it back to draft — otherwise
            # "approved" would mean "someone approved some earlier version of
            # this", which is worse than no approval at all.
            n = conn.execute(
                "UPDATE marketing_drafts SET body=?, content_type=?, topic=?, media_id=?, "
                "status='draft', approved_by=NULL, approved_at=NULL, updated_at=datetime('now') "
                "WHERE id=? AND restaurant_id=?",
                (body, content_type, topic, media_id or None, draft_id, restaurant_id),
            ).rowcount
            conn.commit()
            if not n:
                return {"ok": False, "error": "That draft no longer exists."}
            return {"ok": True, "id": draft_id, "status": "draft"}

        cur = conn.execute(
            "INSERT INTO marketing_drafts (restaurant_id, content_type, topic, body, media_id, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (restaurant_id, content_type, topic, body, media_id or None, user_id),
        )
        conn.commit()
        return {"ok": True, "id": cur.lastrowid, "status": "draft"}
    finally:
        conn.close()


def list_drafts(restaurant_id, limit=40, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT d.id, d.content_type, d.topic, d.body, d.media_id, d.status, "
            "       d.created_at, d.updated_at, d.approved_at, "
            "       m.token AS media_token, u.username AS created_by_name, "
            "       a.username AS approved_by_name "
            "FROM marketing_drafts d "
            "LEFT JOIN marketing_media m ON m.id = d.media_id "
            "LEFT JOIN users u ON u.id = d.created_by "
            "LEFT JOIN users a ON a.id = d.approved_by "
            "WHERE d.restaurant_id=? ORDER BY d.updated_at DESC LIMIT ?",
            (restaurant_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_draft(draft_id, restaurant_id, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT d.*, m.token AS media_token FROM marketing_drafts d "
            "LEFT JOIN marketing_media m ON m.id = d.media_id "
            "WHERE d.id=? AND d.restaurant_id=?",
            (draft_id, restaurant_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


# Roles that may NOT release content. Deny-list rather than allow-list,
# deliberately: an allow-list of {"client", "owner"} would silently lock out
# any role added later, which is the shape of the bug this replaces.
CANNOT_APPROVE = {"member"}


def approve_draft(draft_id, restaurant_id, *, user_id=None, role=None,
                  db_path: str = DB_PATH) -> dict:
    """Release a draft. Invited teammates can write but not publish.

    This used to require role == "owner", which meant NOBODY could approve
    anything: every restaurant's primary login is 'client', and 'owner' is
    reserved for the multi-restaurant account. auth.invite_team_member's
    comment records the same mistake being made and fixed for Team access.
    """
    if role in CANNOT_APPROVE:
        return {"ok": False,
                "error": "Ask the main account to approve this before it goes out."}
    conn = get_conn(db_path)
    try:
        n = conn.execute(
            "UPDATE marketing_drafts SET status='approved', approved_by=?, "
            "approved_at=datetime('now'), updated_at=datetime('now') "
            "WHERE id=? AND restaurant_id=? AND status='draft'",
            (user_id, draft_id, restaurant_id),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    if not n:
        return {"ok": False, "error": "That draft is already approved or no longer exists."}
    return {"ok": True, "id": draft_id, "status": "approved"}


def delete_draft(draft_id, restaurant_id, db_path: str = DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM marketing_drafts WHERE id=? AND restaurant_id=?",
                     (draft_id, restaurant_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}
