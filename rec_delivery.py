"""
rec_delivery.py — a recommendation counts as shown when it is DELIVERED.

rec_ledger.present() records a `shown` event. The periodic emails and the
daily report called it while they were being BUILT: the weekly review
presented its priorities inside weekly_review.build(), which the digest's
preheader, the digest's fallback headline and the digest body each called —
and the month-ready push and the monthly subject line called
monthly_review.build() too, so a push that never showed a priority logged
the monthly email as having shown it. A build is not a delivery: a send can
fail, be suppressed or never be attempted, and a subject line shows nothing.

So a builder STAGES what it renders (stage()), and the code that actually
sends collects the stage around its render and flushes it only once the send
succeeded:

    with rec_delivery.collect() as shown:
        html = render(...)                  # builders call stage()
        result = emails.deliver(...)
    if result.ok:
        shown.flush()                       # rec_ledger.present_many, per surface

Outside a collect() block stage() records nothing — a preview, a web view of
the same review, a subject-line build or a push that only needs a headline
never counts as the email having shown anything.

Also here, because every delivery surface needs them:

  presentable(key)  whether a key is a recommendation an owner can answer,
                    and so belongs in the acceptance trail at all (#10);
  answerable(key)   the `answerable` flag every payload carrying a
                    recommendation sends, so a client knows to offer
                    Done / Not for us / Track for it;
  link(...) / ask_url(...)
                    a dashboard link that names the recommendation it came
                    from (rec=) and where it was read (src=), so a click is
                    recorded as `opened` against that key (#32).

No model, no network. Never raises into a delivery.
"""
import contextlib
import contextvars
from urllib.parse import quote

# Keys that are NOT a recommendation an owner can answer, and so are never
# presented (they would sit in every acceptance denominator as "ignored"
# when no surface ever offered an answer):
#   money:*           the money ranking — WHERE the money is, a module
#                     label with a figure; its action is the one thing
#                     (fix_first), which carries `same_as` back to it.
#   urgent_reviews    "reply to the N low-star reviews": an obligation the
#                     replies themselves discharge, with no subject — a
#                     "Not for us" on it would silence every future
#                     low-star reply prompt for ten years.
#   intraday_pulse:*  "X% behind a typical Friday": news, not advice; the
#                     pulse's advice is its pulse_cut:* staffing move.
NOT_PRESENTED_PREFIXES = ("money:", "intraday_pulse:")
NOT_PRESENTED_KEYS = frozenset({"urgent_reviews"})

_STAGE = contextvars.ContextVar("rec_delivery_stage", default=None)


def presentable(key) -> bool:
    """Whether `key` is a recommendation that may be presented to the
    ledger (see NOT_PRESENTED_*)."""
    k = str(key or "").strip()
    if not k:
        return False
    if k in NOT_PRESENTED_KEYS:
        return False
    return not k.startswith(NOT_PRESENTED_PREFIXES)


def answerable(key) -> bool:
    """The `answerable` flag a payload carries beside `rec_key`: true when
    the key is a recommendation an owner can answer (Done / Not for us /
    Track through POST /recs/event). Bookkeeping keys and the
    not-presented keys above are false."""
    if not presentable(key):
        return False
    try:
        import rec_ledger
        return rec_ledger.counts_in_acceptance(key)
    except Exception:
        return True


def only_presentable(items) -> list:
    """The items (dicts with `key`) that may be presented."""
    return [it for it in (items or []) if it and presentable(it.get("key"))]


class Collected:
    """What one delivery staged: [(restaurant_id, surface, items)]."""

    def __init__(self):
        self.groups = []

    def add(self, restaurant_id, surface, items):
        if restaurant_id and surface and items:
            self.groups.append((restaurant_id, surface, list(items)))

    def keys(self, surface=None) -> list:
        return [it.get("key") for rid, sf, items in self.groups for it in items
                if surface is None or sf == surface]

    def flush(self, user_id=None, db_path=None) -> dict:
        """Present everything staged, one present_many per group. Returns
        {surface: {key: rec_id}}. Never raises."""
        out = {}
        try:
            import rec_ledger
            from models import DB_PATH
        except Exception as e:
            print(f"[rec_delivery] ledger unavailable: {e}")
            return out
        for rid, surface, items in self.groups:
            try:
                got = rec_ledger.present_many(rid, [dict(it, position=it.get("position", i))
                                                    for i, it in enumerate(items)],
                                              surface, user_id=user_id, db_path=db_path or DB_PATH)
                out.setdefault(surface, {}).update(got or {})
            except Exception as e:
                print(f"[rec_delivery] present failed rid={rid} surface={surface}: {e}")
        self.groups = []
        return out


@contextlib.contextmanager
def collect():
    """Collect what the builders stage while this block runs. The caller
    flushes only when the delivery succeeded; leaving the block without a
    flush records nothing."""
    bag = Collected()
    token = _STAGE.set(bag)
    try:
        yield bag
    finally:
        _STAGE.reset(token)


def stage(restaurant_id, surface, items) -> bool:
    """A builder rendered these recommendations for `surface`. Held until
    the delivery around it succeeds; recorded nowhere when nothing is
    delivering (a preview, a subject line, a web view). Items that are not
    presentable are dropped. Returns True when staged."""
    bag = _STAGE.get()
    items = only_presentable(items)
    if bag is None or not items:
        return False
    bag.add(restaurant_id, surface, items)
    return True


def delivering() -> bool:
    """True inside a collect() block."""
    return _STAGE.get() is not None


# ── links that name the recommendation they came from ────────────────────────

def link(url, rec=None, src=None) -> str:
    """`url` with rec=<key>&src=<surface> appended (when there is a key).
    The dashboard records the click as `opened` on that key: hosted_dashboard
    on the page load, or dashboard.html's ?ask= handler when the link also
    asks a question."""
    if not rec:
        return url
    base, frag = (url.split("#", 1) + [""])[:2] if "#" in url else (url, "")
    sep = "&" if "?" in base else "?"
    out = f"{base}{sep}rec={quote(str(rec), safe='')}"
    if src:
        out += f"&src={quote(str(src), safe='')}"
    return out + (f"#{frag}" if frag else "")


def ask_url(prompt, rec=None, src=None) -> str:
    """A link that opens the dashboard and asks that question (dashboard.html
    reads ?ask=), naming the recommendation it came from."""
    import config
    url = f"{config.base_url()}/?ask={quote(prompt or '', safe='')}"
    return link(url, rec, src)


def record_link_open(user, args) -> bool:
    """The dashboard was loaded from a link naming a recommendation
    (?rec=<key>&src=<surface>): record `opened` on that key, once per key,
    surface, login and UTC day. Server-side, so it holds for a link with no
    question in it (an alert's button, an SMS, a report link) and needs no
    client code.

    A link that also asks a question (?ask=) is left to dashboard.html,
    whose ?ask= handler already posts the same open to /recs/event —
    recording it here as well would count one tap twice. An open never
    starts an episode (rec_ledger.NON_OPENING): a key nobody was shown
    records nothing. Never raises."""
    try:
        rec = str((args or {}).get("rec") or "").strip()[:160]
        if not rec or not user or (args or {}).get("ask"):
            return False
        import rec_ledger
        from datetime import datetime
        src = str((args or {}).get("src") or "").strip()
        surface = src if src in rec_ledger.SURFACES else "unknown"
        uid = user.get("id")
        day = datetime.utcnow().strftime("%Y-%m-%d")
        return rec_ledger.record(user["restaurant_id"], rec, "opened", surface=surface, user_id=uid,
                                 role=user.get("role"), meta={"via": "link"},
                                 source_ref=f"link:{surface}:{day}:{uid}")
    except Exception as e:
        print(f"[rec_delivery] link open not recorded: {e}")
        return False


def dashboard_url(rec=None, src=None, path="/") -> str:
    """The dashboard (or a path on it), naming the recommendation."""
    import config
    return link(f"{config.base_url()}{path}", rec, src)
