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

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time - CLAUDE.md's bound-import
    hazard: imported first while a test had models.get_conn patched, the
    bound copy kept that test's database for the rest of the run."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()

log = logging.getLogger(__name__)

MAX_BODY = 6000


def save_draft(restaurant_id, body, *, content_type=None, topic=None, media_id=None,
               draft_id=None, user_id=None, db_path: str = DB_PATH, original_body=None,
               content_log_id=None, draft_ref=None) -> dict:
    """Save (or re-save) a draft. The model's first text is kept as
    `original_body` (memory audit 9/29/26, mkt_edits): given by the caller,
    else the model draft the composer's `draft_ref` / `content_log_id`
    names (marketing_voice), else the body at first save — an edit used to
    overwrite `body` and nothing kept what the model wrote. A later edit
    never replaces it."""
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "There's nothing to save."}
    if len(body) > MAX_BODY:
        return {"ok": False, "error": "That's longer than any platform will take."}
    if media_id:
        from marketing_media import get_media_token
        if not get_media_token(media_id, restaurant_id, db_path=db_path):
            return {"ok": False, "error": "That photo isn't in your library."}

    conn = get_conn(db_path)
    try:
        if draft_id:
            # Editing an approved draft sends it back to draft — otherwise
            # "approved" would mean "someone approved some earlier version of
            # this", which is worse than no approval at all.
            # An expired draft (a quiet-night post whose night has passed —
            # strategy_jobs.expire_quiet_night_drafts) is not revived by an
            # edit: saving it again would make "come in Tuesday" approvable
            # on Thursday.
            n = conn.execute(
                "UPDATE marketing_drafts SET original_body=COALESCE(original_body, body), body=?, content_type=?, "
                "topic=?, media_id=?, "
                "status='draft', approved_by=NULL, approved_at=NULL, updated_at=datetime('now') "
                "WHERE id=? AND restaurant_id=? AND COALESCE(status,'draft') != 'expired'",
                (body, content_type, topic, media_id or None, draft_id, restaurant_id),
            ).rowcount
            conn.commit()
            if not n:
                gone = conn.execute("SELECT status FROM marketing_drafts WHERE id=? AND restaurant_id=?",
                                    (draft_id, restaurant_id)).fetchone()
                # A code as well as the words: the composer clears the id it
                # was holding on either, so the next Save starts a new draft
                # instead of failing forever against this one.
                if gone and gone["status"] == "expired":
                    return {"ok": False, "code": "draft_expired",
                            "error": "That draft expired — its night has passed. Start a new one."}
                return {"ok": False, "code": "draft_gone", "error": "That draft no longer exists."}
            return {"ok": True, "id": draft_id, "status": "draft"}

        # The model draft this one began as, kept by id (model_draft_id) so
        # its approval files the outcome on the run that wrote it (AI cost
        # audit 10/7/26 re-audit #8). By reference only.
        model_draft_id = None
        if draft_ref or content_log_id:
            try:
                import marketing_voice
                m = marketing_voice._draft_by_reference(conn, restaurant_id, "social", draft_ref, content_log_id)
                if m is None and original_body is None:
                    m = marketing_voice._match_draft(conn, restaurant_id, "social", body, draft_id=draft_ref,
                                                     content_log_id=content_log_id)
                model_draft_id = m["id"] if m else None
                if original_body is None:
                    original_body = m["body"] if m else None
            except Exception:
                pass
        cur = conn.execute(
            "INSERT INTO marketing_drafts (restaurant_id, content_type, topic, body, media_id, created_by, "
            "original_body, model_draft_id) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, content_type, topic, body, media_id or None, user_id,
             (original_body or body).strip()[:MAX_BODY], model_draft_id),
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
            "LEFT JOIN marketing_media m ON m.id = d.media_id AND m.restaurant_id = d.restaurant_id "
            "LEFT JOIN users u ON u.id = d.created_by "
            "LEFT JOIN users a ON a.id = d.approved_by "
            "WHERE d.restaurant_id=? ORDER BY d.updated_at DESC LIMIT ?",
            (restaurant_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]



CANNOT_PUBLISH = "Ask the main account to approve this before it goes out."


def may_publish(user) -> bool:
    """Whether this login may put copy on a public feed — now or scheduled.

    Approval was advisory: approve_draft refused a teammate, and every
    publish and schedule route then let the same teammate post the same
    copy directly (MOD-MKT-17). The routes ask this, with the same
    permission approve_draft checks, so there is one rule."""
    from permissions import MARKETING_APPROVE, has_permission
    return has_permission(user, MARKETING_APPROVE)


def approve_draft(draft_id, restaurant_id, *, user_id=None, role=None,
                  db_path: str = DB_PATH, user=None) -> dict:
    """Release a draft. Invited teammates can write but not publish.

    This used to require role == "owner", which meant NOBODY could approve
    anything: every restaurant's primary login is 'client', and 'owner' is
    reserved for the multi-restaurant account. auth.invite_team_member's
    comment records the same mistake being made and fixed for Team access.

    The fix for that was a deny-list, which had the opposite failure mode: a
    role added later was silently PERMITTED to publish. Both directions now
    resolve through permissions.ROLE_PERMISSIONS, where a new role starts
    with nothing and has to be granted MARKETING_APPROVE explicitly.

    An approved draft is a piece that goes out: its text is measured against
    the model's original (marketing_voice.record_final; memory audit
    9/29/26, mkt_edits), with who approved it (`user`, the route's login).
    """
    from permissions import MARKETING_APPROVE, has_permission
    if not has_permission({"role": role}, MARKETING_APPROVE):
        return {"ok": False,
                "error": "Ask the main account to approve this before it goes out."}
    conn = get_conn(db_path)
    try:
        # Approval releases a POST to be published. A guest text or an email
        # (the quiet-night job's old 'guest_sms' drafts, a saved Re-engagement
        # text or Weekly email) has no publish step after it: approving one
        # flipped its status and sent nothing, and the owner believed guests
        # had been texted (Marketing audit AUX-5 / #38). Those are sent from
        # the Campaign Studio, and the refusal says so.
        row = conn.execute("SELECT content_type FROM marketing_drafts WHERE id=? AND restaurant_id=?",
                           (draft_id, restaurant_id)).fetchone()
        if row is not None:
            from marketing import content_channel
            channel = content_channel(row["content_type"])
            if channel != "social":
                return {"ok": False, "code": "send_from_campaigns", "channel": channel,
                        "error": ("A guest text isn't approved here — open it in Campaigns to send it."
                                  if channel == "text" else
                                  "An email isn't approved here — open it in Campaigns to send it.")}
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
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT status FROM marketing_drafts WHERE id=? AND restaurant_id=?",
                               (draft_id, restaurant_id)).fetchone()
        finally:
            conn.close()
        if row and row["status"] == "expired":
            return {"ok": False, "error": "That draft expired — its night has passed, so it can't be approved."}
        return {"ok": False, "error": "That draft is already approved or no longer exists."}
    try:
        conn = get_conn(db_path)
        try:
            d = conn.execute("SELECT body, original_body, model_draft_id FROM marketing_drafts "
                             "WHERE id=? AND restaurant_id=?", (draft_id, restaurant_id)).fetchone()
        finally:
            conn.close()
        if d:
            import marketing_voice
            # draft_id: the model draft it began as, so its run gets the
            # outcome (re-audit #8) — the original alone never found it.
            marketing_voice.record_final(restaurant_id, "social", d["body"], "draft_approved", ref_id=draft_id,
                                         user=user or {"id": user_id, "role": role},
                                         draft_id=d["model_draft_id"],
                                         original_body=d["original_body"] or d["body"], db_path=db_path)
    except Exception as e:
        log.warning("marketing draft %s edit not recorded: %s", draft_id, e)
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
