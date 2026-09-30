"""marketing_voice.py — what the owner does to marketing copy, measured and fed
back (memory audit 9/29/26, workstream M6: mkt_edits, and the marketing half of
"uncaptured").

An edit to a post, a guest text or a newsletter overwrote what the model wrote,
so marketing never learned the owner's voice: the owner stripped the hashtags
and emoji from every caption and signed "— Gia", and the fortieth draft still
had them; the owner rewrote every win-back text shorter and signed it
"–Erik", and the next draft repeated the original. Reply drafts have learned
from the owner's edits since audit #40 (reply_edits); this is the same loop for
every marketing channel.

Two stores, both written at boot by models.init_db:

  marketing_model_drafts  every piece of copy a model drafted for a person
                          (the post composer, the Campaign Studio's text and
                          email) — kept 90 days (ops._RETENTION_DAYS): long
                          enough to match what went out to what was drafted,
                          and to see which drafts were thrown away.
  marketing_edits         one row per piece that WENT OUT (a post published
                          or approved, a text or an email sent): the model's
                          original beside the final text, the edit measured
                          (reply_edits.compare plus the marketing signals —
                          hashtags, emoji, a sign-off), and who sent it
                          (permissions.answer_authority). Kept: it is the
                          owner's voice record, one row per real piece.

What the generators read (voice_block, per channel): a style line from the
account holder's own edits (principal only — a manager's or an admin's are
not the owner's voice), up to three pieces the owner sent in their own words
(fenced), and what the drafts they regenerated instead of using had in common.
The lines are a closed vocabulary; only the fenced examples carry owner text.
"""
import difflib
import json
import re

import reply_edits

CHANNELS = ("social", "text", "email")
CHANNEL_NOUN = {"social": "captions", "text": "guest texts", "email": "emails"}
DRAFTS_KEEP_DAYS = 90            # ops._RETENTION_DAYS["marketing_model_drafts"]
MATCH_HOURS = 6                  # a send without a draft reference matches this user's draft this recent
MATCH_MIN_RATIO = 0.3            # ...when it shares this much of its words with it
REGENERATED_WITHIN_MINUTES = 30  # a newer draft this soon after, and this one never used, is a "no"
EXAMPLES = 3
EXAMPLE_CHARS = 500
SAME_PIECE_DAYS = 7              # the same final text on a channel is one piece within this

_HASHTAG = re.compile(r"(?<![\w&])#\w+")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿\U0001F1E6-\U0001F1FF]")
# A last line that is only a name after a dash: "— Gia", "-Erik", "~ The Rossi family".
_SIGNOFF = re.compile(r"(?:^|\n)\s*[—–~-]{1,2}\s*[A-Z][\w'.&]*(?:\s+[A-Z][\w'.&]*){0,3}\s*$")

# The marketing signals, in the order a note lists them.
SIGNAL_TEXT = {
    "removed_hashtags": "they take the hashtags out",
    "added_hashtags": "they add hashtags",
    "removed_emoji": "they take the emoji out",
    "added_emoji": "they add emoji",
    "added_signoff": "they end it with their own sign-off line",
    "removed_signoff": "they take the sign-off off",
    "shortened": "they cut the draft down",
    "lengthened": "they add to the draft",
    "removed_exclamations": "they take out exclamation marks",
    "added_exclamations": "they add exclamation marks",
    "removed_invitation": "they drop the invitation to come in",
    "added_invitation": "they add an invitation to come in",
}
# What a thrown-away draft was like (regenerated_note), the same closed way.
DRAFT_SIGNAL_TEXT = {
    "hashtags": "carry hashtags",
    "emoji": "use emoji",
    "exclamations": "use exclamation marks",
    "long": "run longer than the pieces they send",
}


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def _words(text):
    return re.findall(r"[A-Za-z0-9']+", str(text or "").lower())


def _has(pattern, text):
    return bool(pattern.search(str(text or "")))


def compare(original, final) -> dict:
    """reply_edits.compare plus what marketing copy adds: hashtags, emoji and
    a sign-off line, taken out or put in. Closed vocabulary, deterministic."""
    out = reply_edits.compare(original, final)
    signals = list(out["signals"])
    for name, pat in (("hashtags", _HASHTAG), ("emoji", _EMOJI), ("signoff", _SIGNOFF)):
        a, b = _has(pat, original), _has(pat, final)
        if a and not b:
            signals.append(f"removed_{name}")
        elif b and not a:
            signals.append(f"added_{name}")
    if signals and out["category"] == "unchanged":
        # The words match and the voice does not: hashtags or emoji out is
        # an edit (the reply rule for a punctuation-only change).
        out["category"] = "light"
    out["signals"] = [s for s in signals if s in SIGNAL_TEXT]
    return out


def draft_signals(text, typical_words=None) -> list:
    """What one drafted piece is like, closed vocabulary — never its words."""
    t = str(text or "")
    out = []
    if len(_HASHTAG.findall(t)) >= 2:
        out.append("hashtags")
    if _has(_EMOJI, t):
        out.append("emoji")
    if "!" in t:
        out.append("exclamations")
    if typical_words and len(_words(t)) > typical_words * 1.3:
        out.append("long")
    return out


# ── capture ─────────────────────────────────────────────────────────────────

def record_draft(restaurant_id, channel, body, source, user_id=None, content_log_id=None, db_path=None):
    """One piece of copy a model drafted. Returns its id (the `draft_ref`
    the clients send back with the piece that goes out) or None; never
    raises into the draft.

    A draft made through view-as carries no person (memory audit 9/29/26,
    "view_as"): the session answers as the owner's own login, and an
    admin's drafts thrown away for another (regenerated) or matched to a
    send by person (_match_draft) are support at work, not the owner's
    "no". It is still kept, and still matched by its draft_ref."""
    body = str(body or "").strip()
    if not restaurant_id or channel not in CHANNELS or not body:
        return None
    try:
        from permissions import acting_via
        if acting_via():
            user_id = None
    except Exception:
        pass
    try:
        conn = get_conn(db_path)
        try:
            cur = conn.execute("INSERT INTO marketing_model_drafts (restaurant_id, channel, source, body, "
                               "content_log_id, user_id) VALUES (?,?,?,?,?,?)",
                               (restaurant_id, channel, source, body[:6000], content_log_id, user_id))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        print(f"[marketing_voice] draft not recorded for {restaurant_id}: {e}")
        return None


def _match_draft(conn, restaurant_id, channel, final, draft_id=None, content_log_id=None, user_id=None):
    """The model draft this final piece came from: by its reference, by the
    generated content-log row, else this person's latest draft on the
    channel within MATCH_HOURS that shares MATCH_MIN_RATIO of its words —
    or None (written from blank, or no draft to match)."""
    row = None
    if draft_id:
        try:
            row = conn.execute("SELECT * FROM marketing_model_drafts WHERE id=? AND restaurant_id=? AND channel=?",
                               (int(draft_id), restaurant_id, channel)).fetchone()
        except (TypeError, ValueError):
            row = None
    if row is None and content_log_id:
        row = conn.execute("SELECT * FROM marketing_model_drafts WHERE restaurant_id=? AND content_log_id=? "
                           "ORDER BY id DESC LIMIT 1", (restaurant_id, content_log_id)).fetchone()
    if row is None:
        cands = conn.execute(
            "SELECT * FROM marketing_model_drafts WHERE restaurant_id=? AND channel=? AND used_at IS NULL "
            "AND (? IS NULL OR user_id IS NULL OR user_id=?) AND created_at >= datetime('now', ?) "
            "ORDER BY id DESC LIMIT 5", (restaurant_id, channel, user_id, user_id,
                                         f"-{int(MATCH_HOURS)} hours")).fetchall()
        fw = _words(final)
        for c in cands:
            ratio = difflib.SequenceMatcher(None, _words(c["body"]), fw, autojunk=False).ratio()
            if ratio >= MATCH_MIN_RATIO:
                row = c
                break
    return row


def record_final(restaurant_id, channel, final_body, source, ref_id=None, user=None, draft_id=None,
                 content_log_id=None, original_body=None, db_path=None):
    """A piece that went out: `final_body` on `channel` (source: post_publish
    | draft_approved | campaign | winback | newsletter; `ref_id` the row it
    went out as). The model's original is `original_body` when the caller
    has it, else the matched model draft; the edit is measured against it
    and who sent it recorded. One row per (source, ref_id), and one per
    identical text on a channel within SAME_PIECE_DAYS. Returns the
    measured edit or None; never raises into the send."""
    final_body = str(final_body or "").strip()
    if not restaurant_id or channel not in CHANNELS or not final_body:
        return None
    try:
        from permissions import answer_authority
        u = user or {}
        view_as = bool(u.get("acting_admin_id") or u.get("acting_admin_role")
                       or (u.get("device_type") or "") == "admin-view-as")
        authority = answer_authority(u) if u else None
        uid = u.get("acting_admin_id") if (view_as and u.get("acting_admin_id")) else u.get("id")
        conn = get_conn(db_path)
        try:
            if conn.execute("SELECT 1 FROM marketing_edits WHERE restaurant_id=? AND channel=? AND final_body=? "
                            "AND created_at >= datetime('now', ?) LIMIT 1",
                            (restaurant_id, channel, final_body, f"-{int(SAME_PIECE_DAYS)} days")).fetchone():
                return None
            matched = None
            if original_body is None:
                matched = _match_draft(conn, restaurant_id, channel, final_body, draft_id=draft_id,
                                       content_log_id=content_log_id, user_id=u.get("id"))
                original_body = matched["body"] if matched else None
            summary = compare(original_body, final_body) if (original_body or "").strip() else None
            conn.execute(
                "INSERT OR IGNORE INTO marketing_edits (restaurant_id, channel, source, ref_id, draft_id, "
                "original_body, final_body, edit_distance, edit_category, edit_signals, words_before, words_after, "
                "user_id, authority, via) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (restaurant_id, channel, source, ref_id, matched["id"] if matched else None,
                 (original_body or None), final_body[:6000],
                 summary["distance"] if summary else None, summary["category"] if summary else "written",
                 json.dumps(summary["signals"]) if summary else "[]",
                 summary["words_before"] if summary else None, len(_words(final_body)),
                 uid, authority, "view_as" if view_as else ("normal" if u else None)))
            if matched:
                conn.execute("UPDATE marketing_model_drafts SET used_at=datetime('now') WHERE id=?",
                             (matched["id"],))
            conn.commit()
            return summary
        finally:
            conn.close()
    except Exception as e:
        print(f"[marketing_voice] {source} not recorded for {restaurant_id}: {e}")
        return None


# ── what the generators read ────────────────────────────────────────────────

# How far back the OWNER'S EDITS line reads edited pieces (memory re-audit
# 9/29/26, LOOPS-4 — reply_edits' rule): the edits that taught a lesson,
# not the newest pieces of any kind, which pushed them out once the
# generator followed them.
NOTE_DAYS = 365


def _voice_sql():
    """Whose sent pieces are the restaurant's voice: the account holder's, or
    a login holding the marketing-voice grant (permissions.MARKETING_VOICE,
    read at the time of use — a revoke takes them back out; memory re-audit
    9/29/26, LOOPS-15); never a piece sent through view-as."""
    return ("COALESCE(via, '') != 'view_as' AND (authority='principal' OR (authority='delegate' AND user_id IN "
            "(SELECT g.user_id FROM permission_grants g WHERE g.restaurant_id=marketing_edits.restaurant_id "
            "AND g.permission='marketing.voice')))")


def _principal_rows(conn, restaurant_id, channel, limit=40, edited_only=False, days=None):
    """The pieces sent on `channel` in the restaurant's voice (_voice_sql),
    newest first. `edited_only` keeps the ones the owner changed or wrote
    (the style line and the examples); `days` bounds how far back."""
    where = ["restaurant_id=?", "channel=?"]
    args = [restaurant_id, channel]
    try:
        conn.execute("SELECT 1 FROM permission_grants LIMIT 1")
        where.append(_voice_sql())
    except Exception:                           # a database without the auth schema
        where.append("authority='principal' AND COALESCE(via, '') != 'view_as'")
    if edited_only:
        where.append("edit_category IN ('light','heavy','rewrite','written')")
    if days:
        where.append(f"created_at >= datetime('now', '-{int(days)} days')")
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM marketing_edits WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
        (*args, int(limit))).fetchall()]


def style_note(rows, channel) -> str:
    """The OWNER'S EDITS line for one channel, from the account holder's
    sent pieces (newest first): the signals at least reply_edits'
    MIN_SIGNAL_SHARE of their edited pieces share (and at least MIN_EDITS of
    them), and the typical length they send. "" below MIN_EDITS edited
    pieces. Deterministic."""
    edited = [r for r in rows if r.get("edit_category") in ("light", "heavy", "rewrite")][:reply_edits.NOTE_WINDOW]
    if len(edited) < reply_edits.MIN_EDITS:
        return ""
    counts = {}
    for r in edited:
        try:
            sigs = set(json.loads(r.get("edit_signals") or "[]"))
        except (TypeError, ValueError):
            sigs = set()
        for s in sigs:
            if s in SIGNAL_TEXT:
                counts[s] = counts.get(s, 0) + 1
    need = max(reply_edits.MIN_EDITS, int(len(edited) * reply_edits.MIN_SIGNAL_SHARE + 0.999))
    bits = [SIGNAL_TEXT[s] for s in SIGNAL_TEXT if counts.get(s, 0) >= need][:5]
    heavy = sum(1 for r in edited if r.get("edit_category") in ("heavy", "rewrite"))
    if heavy >= need:
        bits.append("they rewrite most of it in their own words")
    lengths = sorted(int(r.get("words_after") or 0) for r in edited if r.get("words_after"))
    typical = lengths[len(lengths) // 2] if lengths else None
    if not bits and not typical:
        return ""
    noun = CHANNEL_NOUN.get(channel, "pieces")
    line = (f"OWNER'S EDITS — measured from the last {len(edited)} {noun} the owner changed before they went "
            f"out, not guessed:")
    if bits:
        line += " " + "; ".join(bits) + "."
    if typical:
        line += f" The {noun} they send run about {typical} words."
    return line + " Write this one the way they finish theirs."


def examples(rows, limit=EXAMPLES) -> list:
    """Up to `limit` pieces the owner sent in their own words — edited or
    written from blank — newest first. Never a piece sent as drafted: that is
    the model's own text, and offering it as "the owner's own words" fed the
    generator its own style back (memory re-audit 9/29/26, QUALITY-18); those
    count only as approvals (approved_as_drafted)."""
    edited = [r for r in rows if r.get("edit_category") in ("light", "heavy", "rewrite", "written")]
    return [str(r["final_body"])[:EXAMPLE_CHARS] for r in edited[:limit]]


def approved_as_drafted(rows) -> int:
    """How many of `rows` went out exactly as drafted — an approval signal,
    never an example of the owner's voice."""
    return sum(1 for r in rows if r.get("edit_category") == "unchanged")


def regenerated(conn, restaurant_id, channel, days=DRAFTS_KEEP_DAYS) -> list:
    """The drafts on this channel a person threw away for another within
    REGENERATED_WITHIN_MINUTES and never used — a regeneration is a "no" to
    the draft, as it is for replies (LOOPS-19). Their bodies, newest first."""
    rows = conn.execute(
        "SELECT d.body FROM marketing_model_drafts d WHERE d.restaurant_id=? AND d.channel=? AND d.used_at IS NULL "
        "AND d.user_id IS NOT NULL AND d.created_at >= datetime('now', ?) "
        "AND EXISTS (SELECT 1 FROM marketing_model_drafts n WHERE n.restaurant_id=d.restaurant_id "
        "  AND n.channel=d.channel AND n.user_id=d.user_id AND n.id > d.id "
        "  AND n.created_at <= datetime(d.created_at, ?)) ORDER BY d.id DESC LIMIT 30",
        (restaurant_id, channel, f"-{int(days)} days", f"+{int(REGENERATED_WITHIN_MINUTES)} minutes")).fetchall()
    return [r["body"] for r in rows]


def regenerated_note(thrown, sent, channel) -> str:
    """What the drafts the owner regenerated tended to do that the pieces
    they sent do not — reply_edits.rejection_note's rule, marketing's
    vocabulary. "" below its floor."""
    lengths = sorted(len(_words(s)) for s in sent if s)
    typical = lengths[len(lengths) // 2] if lengths else None
    rej = [set(draft_signals(t, typical)) for t in thrown]
    ok = [set(draft_signals(s, typical)) for s in sent]
    if len(rej) < reply_edits.REJECTION_MIN:
        return ""
    named = []
    for sig, text in DRAFT_SIGNAL_TEXT.items():
        share = sum(1 for s in rej if sig in s) / len(rej)
        if share < reply_edits.REJECTION_SHARE:
            continue
        if len(ok) >= reply_edits.REJECTION_MIN:
            base = sum(1 for s in ok if sig in s) / len(ok)
            if share - base < reply_edits.REJECTION_MARGIN:
                continue
        named.append(text)
    if not named:
        return ""
    return (f"The owner regenerated {len(rej)} {CHANNEL_NOUN.get(channel, 'drafts')} drafts in the last "
            f"{DRAFTS_KEEP_DAYS} days rather than use them; those drafts tended to " + "; ".join(named) +
            ". Avoid that here.")


def voice_block(restaurant_id, channel, db_path=None) -> str:
    """The prompt block for a generator on `channel`: the owner's style line,
    up to three pieces they sent in their own words (fenced — their voice,
    never a source for an offer, a date or a price), and what the drafts
    they threw away had in common. "" when nothing is known yet — and for a
    restaurant that does not learn for itself (models.learns_for_itself: a
    demo, or an account an admin excluded). Never raises."""
    if not restaurant_id or channel not in CHANNELS:
        return ""
    try:
        import models
        if not models.learns_for_itself(restaurant_id):
            return ""
        from ai_guard import wrap_untrusted
        conn = get_conn(db_path)
        try:
            rows = _principal_rows(conn, restaurant_id, channel)
            edited = _principal_rows(conn, restaurant_id, channel, limit=reply_edits.NOTE_WINDOW,
                                     edited_only=True, days=NOTE_DAYS)
            thrown = regenerated(conn, restaurant_id, channel)
        finally:
            conn.close()
    except Exception as e:
        print(f"[marketing_voice] voice unreadable for {restaurant_id}/{channel}: {e}")
        return ""
    parts = []
    note = style_note(edited, channel)
    if note:
        parts.append(note)
    ex = examples(edited)
    noun = CHANNEL_NOUN.get(channel, "pieces")
    if ex:
        parts.append(f"{len(ex)} {noun} the owner sent in their own words — their voice to match, not text to "
                     "reuse: never copy an offer, a price, a date or an event from them:\n"
                     + "\n".join(wrap_untrusted(e) for e in ex))
    kept = approved_as_drafted(rows)
    if kept:
        # Approval, not voice: the drafts themselves are never shown back.
        parts.append(f"The owner sent {kept} of the recent {noun} as drafted (approved as drafted — the "
                     "style of those drafts is acceptable to them).")
    regen = regenerated_note(thrown, [r["final_body"] for r in rows], channel)
    if regen:
        parts.append(regen)
    return ("\n\nTHE OWNER'S VOICE\n" + "\n".join(parts) + "\n") if parts else ""


def last_sent(restaurant_id, channel, source, days=180, db_path=None):
    """The last piece the account holder sent on `source` (a win-back text)
    in the last `days`, or None — so the next draft starts from their own
    words rather than a fixed template (guest_campaign_drafts.sent_message
    was written and never read)."""
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT final_body FROM marketing_edits WHERE restaurant_id=? AND channel=? AND source=? "
                "AND authority='principal' AND COALESCE(via, '') != 'view_as' AND created_at >= datetime('now', ?) "
                "ORDER BY id DESC LIMIT 1", (restaurant_id, channel, source, f"-{int(days)} days")).fetchone()
        finally:
            conn.close()
        return row["final_body"] if row else None
    except Exception:
        return None


def summary_lines(restaurant_id, db_path=None) -> list:
    """The owner's marketing voice as memory lines (marketing.memory_lines):
    one closed-vocabulary style line per channel with a pattern. Never raises."""
    out = []
    try:
        conn = get_conn(db_path)
        try:
            for ch in CHANNELS:
                note = style_note(_principal_rows(conn, restaurant_id, ch, limit=reply_edits.NOTE_WINDOW,
                                                  edited_only=True, days=NOTE_DAYS), ch)
                if note:
                    out.append(note)
        finally:
            conn.close()
    except Exception:
        return []
    return out
