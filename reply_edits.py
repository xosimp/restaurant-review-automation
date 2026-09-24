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


def style_note(summaries) -> str:
    """The drafter's OWNER'S EDITS block, from the most recent edit
    summaries (newest first): the signals at least MIN_SIGNAL_SHARE of the
    edited replies share (and at least MIN_EDITS of them), and the typical
    length the owner leaves. "" below MIN_EDITS edited replies — a pattern
    needs more than one afternoon's edits. Deterministic: the same rows give
    the same words, so a stored prompt fingerprint holds."""
    edited = [s for s in (summaries or []) if s and s.get("category") in ("light", "heavy", "rewrite")][:NOTE_WINDOW]
    if len(edited) < MIN_EDITS:
        return ""
    counts = {}
    for s in edited:
        for sig in set(s.get("signals") or []):
            if sig in SIGNAL_TEXT:
                counts[sig] = counts.get(sig, 0) + 1
    need = max(MIN_EDITS, int(len(edited) * MIN_SIGNAL_SHARE + 0.999))
    common = [sig for sig in SIGNAL_TEXT if counts.get(sig, 0) >= need][:MAX_NOTE_SIGNALS]
    heavy = sum(1 for s in edited if s.get("category") in ("heavy", "rewrite"))
    bits = [SIGNAL_TEXT[s] for s in common]
    if heavy >= need:
        bits.append("they rewrite most of it in their own words")
    lengths = sorted(int(s.get("words_after") or 0) for s in edited if s.get("words_after"))
    typical = lengths[len(lengths) // 2] if lengths else None
    if not bits and typical is None:
        return ""
    line = (f"\nOWNER'S EDITS — measured from the last {len(edited)} drafts the owner changed before "
            f"approving, not guessed:")
    if bits:
        line += " " + "; ".join(bits) + "."
    if typical:
        line += f" The replies they approve run about {typical} words."
    return line + " Write this draft the way they finish theirs.\n"
