"""
reply_edits.py — what the owner changes in a drafted reply, measured.

reviews.original_draft has kept the model's text since the first edit
(suggested vs chosen, audit #41), and nothing ever compared it with the
reply the owner approved. So the drafter kept writing the reply the owner
keeps rewriting: too long, an exclamation mark they always take out, an
apology they always cut. Recommendation ROI audit #40.

Two pure functions, no I/O:

  compare(original, final)   one approved reply's edit, summarised: a word
                             edit distance (0 = unchanged, 1 = rewritten),
                             a category, the word counts, and signals from a
                             closed vocabulary — never free text, so nothing
                             an owner or a guest wrote reaches a prompt
                             through here;
  style_note(summaries)      a short, deterministic "OWNER'S EDITS" block
                             for the drafter's prompt, from the signals a
                             majority of the owner's recent edits share —
                             or "" below MIN_EDITS edited replies.

The rows are written at approval (models.record_reply_edit) and read by
drafter.draft_response; the few-shot examples prefer an edited reply
(models.get_approved_examples), which is the owner's own words.
"""
import difflib
import re

# Below this word distance an approved draft is "unchanged": a typo fixed
# or a word swapped is not a correction of the drafter's voice.
UNCHANGED_BELOW = 0.02
LIGHT_BELOW = 0.25          # a sentence adjusted
HEAVY_BELOW = 0.60          # most of it changed; above this, a rewrite
CATEGORIES = ("unchanged", "light", "heavy", "rewrite")

# A signal counts toward the style note only when this many of the owner's
# edited replies show it AND it is at least half of them.
MIN_EDITS = 3
MIN_SIGNAL_SHARE = 0.5
MAX_NOTE_SIGNALS = 4
NOTE_WINDOW = 12            # the most recent edited replies read

_APOLOGY = re.compile(r"\b(sorry|apolog\w*|regret)\b", re.I)
_INVITE = re.compile(r"\b(come back|visit (us )?again|see you (again|soon|next)|join us|hope to see|stop (back|by))\b",
                     re.I)
_WORD = re.compile(r"[A-Za-z0-9']+")

# signal -> how the note says it. Order is the order the note lists them.
SIGNAL_TEXT = {
    "shortened": "they cut the draft down",
    "lengthened": "they add to the draft",
    "removed_exclamations": "they take out exclamation marks",
    "added_exclamations": "they add exclamation marks",
    "removed_apology": "they remove the apology",
    "added_apology": "they add an apology",
    "removed_invitation": "they drop the invitation to come back",
    "added_invitation": "they add an invitation to come back",
}


def _words(text):
    return _WORD.findall(str(text or "").lower())


def compare(original, final) -> dict:
    """One edit, summarised. `original` is the model's draft as it stood
    before the first edit; `final` the reply the owner approved.
    {"distance", "category", "words_before", "words_after", "signals"}."""
    a, b = _words(original), _words(final)
    if not a and not b:
        ratio = 1.0
    else:
        ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    distance = round(1.0 - ratio, 3)
    if distance < UNCHANGED_BELOW:
        category = "unchanged"
    elif distance < LIGHT_BELOW:
        category = "light"
    elif distance < HEAVY_BELOW:
        category = "heavy"
    else:
        category = "rewrite"
    # Punctuation is voice too. The distance counts words only, so an owner
    # who took every "!" out of every draft — the same words, a different
    # tone — read as "unchanged" and the pattern never reached the note
    # (re-audit C11). A punctuation-only change is a light edit.
    ea, eb = str(original or "").count("!"), str(final or "").count("!")
    punct = "removed_exclamations" if (ea and not eb) else ("added_exclamations" if (eb and not ea) else None)
    if category == "unchanged" and punct:
        category = "light"
    signals = []
    if category != "unchanged":
        before, after = len(a), len(b)
        if before and after <= before * 0.8 and before - after >= 5:
            signals.append("shortened")
        elif before and after >= before * 1.2 and after - before >= 5:
            signals.append("lengthened")
        if punct:
            signals.append(punct)
        pa, pb = bool(_APOLOGY.search(str(original or ""))), bool(_APOLOGY.search(str(final or "")))
        if pa and not pb:
            signals.append("removed_apology")
        elif pb and not pa:
            signals.append("added_apology")
        ia, ib = bool(_INVITE.search(str(original or ""))), bool(_INVITE.search(str(final or "")))
        if ia and not ib:
            signals.append("removed_invitation")
        elif ib and not ia:
            signals.append("added_invitation")
    return {"distance": distance, "category": category, "words_before": len(a), "words_after": len(b),
            "signals": signals}


# The signals that say something about the owner's voice whatever the star
# rating: a note built from another band's edits carries only these
# (memory audit 9/29/26, reply_voice) — "they cut it down" learned on 5-star
# thank-yous is no guide to a 1-star apology, and neither is the length.
BAND_FREE_SIGNALS = ("removed_exclamations", "added_exclamations")


def style_note(summaries, only=None, with_length=True, scope="") -> str:
    """The drafter's OWNER'S EDITS block, from the most recent edit
    summaries (newest first): the signals at least MIN_SIGNAL_SHARE of the
    edited replies share (and at least MIN_EDITS of them), and the typical
    length the owner leaves. "" below MIN_EDITS edited replies — a pattern
    needs more than one afternoon's edits. Deterministic: the same rows give
    the same words, so a stored prompt fingerprint holds.

    `only`: the signals this note may name (BAND_FREE_SIGNALS for a note
    borrowed from other bands); `with_length` False drops the length
    sentence and "rewrite" line; `scope` names what the edits were measured
    on ("replies to 1-2★ reviews")."""
    edited = [s for s in (summaries or []) if s and s.get("category") in ("light", "heavy", "rewrite")][:NOTE_WINDOW]
    if len(edited) < MIN_EDITS:
        return ""
    allowed = tuple(only) if only is not None else tuple(SIGNAL_TEXT)
    counts = {}
    for s in edited:
        for sig in set(s.get("signals") or []):
            if sig in SIGNAL_TEXT and sig in allowed:
                counts[sig] = counts.get(sig, 0) + 1
    need = max(MIN_EDITS, int(len(edited) * MIN_SIGNAL_SHARE + 0.999))
    common = [sig for sig in SIGNAL_TEXT if counts.get(sig, 0) >= need][:MAX_NOTE_SIGNALS]
    heavy = sum(1 for s in edited if s.get("category") in ("heavy", "rewrite"))
    bits = [SIGNAL_TEXT[s] for s in common]
    if with_length and heavy >= need:
        bits.append("they rewrite most of it in their own words")
    typical = None
    if with_length:
        lengths = sorted(int(s.get("words_after") or 0) for s in edited if s.get("words_after"))
        typical = lengths[len(lengths) // 2] if lengths else None
    if not bits and typical is None:
        return ""
    line = (f"\nOWNER'S EDITS — measured from the last {len(edited)} drafts the owner changed before "
            f"approving{(' (' + scope + ')') if scope else ''}, not guessed:")
    if bits:
        line += " " + "; ".join(bits) + "."
    if typical:
        line += f" The replies they approve run about {typical} words."
    return line + " Write this draft the way they finish theirs.\n"


# ── drafts the owner turned down (memory audit 9/29/26, rejected_drafts) ────
#
# A draft the owner regenerated is a "no" to that draft. Its words are not
# kept (reply_draft_rejections holds a hash and these signals, 90 days), so
# what the drafter hears is only what the rejected drafts had in common,
# from the same closed vocabulary style of note: never free text.

# The word range the drafter asks for, by star band (drafter.draft_response's
# length notes), with a little give: a draft past the top is "long".
DRAFT_WORD_RANGE = {(1, 2): (50, 90), (3,): (35, 65), (4, 5): (20, 45)}
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿\U0001F1E6-\U0001F1FF]")
DRAFT_SIGNAL_TEXT = {
    "long": "run longer than they want",
    "short": "run shorter than they want",
    "exclamations": "use exclamation marks",
    "apology": "apologise",
    "invitation": "invite the guest back",
    "emoji": "use emoji",
}
REJECTION_MIN = 3            # rejected drafts in the band before a line is said
REJECTION_SHARE = 0.6        # ...and at least this share of them show the signal
REJECTION_MARGIN = 0.3       # ...this much more often than the replies they approve


def _band_of(rating):
    try:
        r = int(rating)
    except (TypeError, ValueError):
        return None
    return next((b for b in DRAFT_WORD_RANGE if r in b), None)


def draft_signals(text, rating=None) -> list:
    """What one draft is like, in DRAFT_SIGNAL_TEXT's closed vocabulary —
    never its words. Length is judged against the range the drafter asks
    for at this star rating (no rating, no length signal)."""
    t = str(text or "")
    out = []
    band = _band_of(rating)
    if band:
        lo, hi = DRAFT_WORD_RANGE[band]
        n = len(_words(t))
        if n > hi:
            out.append("long")
        elif n and n < lo:
            out.append("short")
    if "!" in t:
        out.append("exclamations")
    if _APOLOGY.search(t):
        out.append("apology")
    if _INVITE.search(t):
        out.append("invitation")
    if _EMOJI.search(t):
        out.append("emoji")
    return out


def rejection_note(rejected, approved=(), scope="") -> str:
    """One line for the OWNER'S EDITS block: what the drafts this owner
    regenerated tended to do that the replies they approve do not. Each of
    `rejected` / `approved` is a list of draft_signals lists (one per
    draft). "" below REJECTION_MIN rejected drafts or with nothing in
    common. A signal is named when REJECTION_SHARE of the rejected drafts
    show it and — once there are REJECTION_MIN approved replies to compare
    with — REJECTION_MARGIN more of them than of the approved. Deterministic."""
    rejected = [set(s or ()) for s in (rejected or [])]
    approved = [set(s or ()) for s in (approved or [])]
    if len(rejected) < REJECTION_MIN:
        return ""
    named = []
    for sig in DRAFT_SIGNAL_TEXT:
        share = sum(1 for s in rejected if sig in s) / len(rejected)
        if share < REJECTION_SHARE:
            continue
        if len(approved) >= REJECTION_MIN:
            base = sum(1 for s in approved if sig in s) / len(approved)
            if share - base < REJECTION_MARGIN:
                continue
        named.append(DRAFT_SIGNAL_TEXT[sig])
    if not named:
        return ""
    return (f"\nThe owner regenerated {len(rejected)} drafts{(' (' + scope + ')') if scope else ''} in the last "
            f"90 days rather than send them; those drafts tended to " + "; ".join(named[:MAX_NOTE_SIGNALS])
            + ". Avoid that here.\n")
