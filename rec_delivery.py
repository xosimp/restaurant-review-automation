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
                    from (rec=), where it was read (src=) and the location
                    it is about (rid=), so a click is recorded as `opened`
                    against that key on that location (#32, re-audit C10);
  present_now(...)  a route that IS the delivery (the JSON a screen
                    renders) presents what it serves, now;
  when_pushed(...)  the push.fire_push `on_delivered` hook: a push presents
                    what it showed once a phone took it — never on queueing,
                    never for a push no device received (re-audit C2/C4).

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
#   intraday_pulse:*  "X% behind a typical Friday": news, not advice.
#
# And the keys only ever shown where nothing can answer them (re-audit C8):
# an episode no surface can close sits in every acceptance figure as
# "ignored", which reads as advice the owner turned down.
#   pulse_cut:*       the pre-dinner pulse's staffing move — push only, and
#                     the app offers nothing on a push but the tap;
#   quiet_night:*     the quiet-night heads-up — push only, the same;
#   digest_move:*     the weekly digest's model-written move — email only,
#                     with an "Ask about this" link and nothing else;
#   monthly_move:*    the monthly email's next move — a setup step the work
#                     itself discharges (approve the drafts, build the first
#                     schedule), shown in the email and nowhere else.
# Give one of these an answer path on a client and it can come back here.
NOT_PRESENTED_PREFIXES = ("money:", "intraday_pulse:", "pulse_cut:", "quiet_night:", "digest_move:",
                          "monthly_move:")
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


def present_now(restaurant_id, surface, items, user_id=None, db_path=None) -> dict:
    """A route that is itself the delivery (the JSON a screen renders)
    presents what it serves, now: the presentable items only, one
    present_many. Returns rec_ledger's {key: rec_id or None} (None: the
    owner already answered it). Never raises."""
    items = only_presentable(items)
    if not restaurant_id or not items:
        return {}
    try:
        import rec_ledger
        from models import DB_PATH
        return rec_ledger.present_many(restaurant_id, [dict(it, position=it.get("position", i))
                                                       for i, it in enumerate(items)],
                                       surface, user_id=user_id, db_path=db_path or DB_PATH) or {}
    except Exception as e:
        print(f"[rec_delivery] present failed rid={restaurant_id} surface={surface}: {e}")
        return {}


def when_pushed(restaurant_id, surface, items, user_id=None, db_path=None):
    """The `on_delivered` hook for push.fire_push: presents `items` on
    `surface` once a phone has actually taken the notification — never for
    a push no device received. None when there is nothing presentable (the
    push goes out without a hook)."""
    items = only_presentable(items)
    if not restaurant_id or not items:
        return None
    return lambda: present_now(restaurant_id, surface, items, user_id=user_id, db_path=db_path)


# ── links that name the recommendation they came from ────────────────────────

def link(url, rec=None, src=None, rid=None) -> str:
    """`url` with rec=<key>&src=<surface>[&rid=<restaurant>] appended (when
    there is a presentable key). The dashboard records the click as `opened`
    on that key when the page loads (record_link_open), with or without a
    question in the link.

    `rid` names the restaurant the recommendation belongs to: a group
    owner's session sits on one location, and a tap on another location's
    alert button was recorded against the session's location (re-audit
    C10). A key that is never presented (presentable) is not carried — an
    open with no episode behind it records nothing anyway."""
    if not rec or not presentable(rec):
        return url
    base, frag = (url.split("#", 1) + [""])[:2] if "#" in url else (url, "")
    sep = "&" if "?" in base else "?"
    out = f"{base}{sep}rec={quote(str(rec), safe='')}"
    if src:
        out += f"&src={quote(str(src), safe='')}"
    if rid:
        try:
            out += f"&rid={int(rid)}"
        except (TypeError, ValueError):
            pass
    return out + (f"#{frag}" if frag else "")


def ask_url(prompt, rec=None, src=None, rid=None) -> str:
    """A link that opens the dashboard and asks that question (dashboard.html
    reads ?ask=), naming the recommendation it came from."""
    import config
    url = f"{config.base_url()}/?ask={quote(prompt or '', safe='')}"
    return link(url, rec, src, rid)


def _may_open_for(user, rid) -> bool:
    """Whether this login may act on location `rid`: its own session's
    location, or — for a login allowed to switch locations — another
    location of the same group (the rule home_brief's location list uses)."""
    try:
        if int(rid) == int(user.get("restaurant_id") or 0):
            return True
        from permissions import LOCATION_SWITCH, has_permission
        if not has_permission(user, LOCATION_SWITCH):
            return False
        from models import get_restaurant, get_location_group
        base = get_restaurant(user.get("base_restaurant_id") or user["restaurant_id"])
        if not base or not getattr(base, "location_group", None):
            return False
        return any(int(loc["id"]) == int(rid)
                   for loc in get_location_group(base.location_group, owner_email=base.owner_email))
    except Exception:
        return False


def local_day(restaurant_id) -> str:
    """The restaurant's own calendar day, "YYYY-MM-DD" — a Chicago evening
    is one day, not two UTC ones. UTC when the restaurant can't be read."""
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id).strftime("%Y-%m-%d")
    except Exception:
        from datetime import datetime
        return datetime.utcnow().strftime("%Y-%m-%d")


def open_source_ref(restaurant_id, surface, user_id) -> str:
    """The dedupe for a link open: one per key, surface, login and the
    restaurant's own day."""
    return f"link:{surface}:{local_day(restaurant_id)}:{user_id}"


def record_link_open(user, args) -> bool:
    """The dashboard was loaded from a link naming a recommendation
    (?rec=<key>&src=<surface>[&rid=<restaurant>]): record `opened` on that
    key, once per key, surface, login and local day. Server-side, so it
    holds for every keyed link — an alert's button, an SMS, a report link,
    and a link that also asks a question (?ask=): that one used to be left
    to dashboard.html, which posted /recs/event on every load with no
    dedupe, so two taps were two opens (re-audit C14).

    The open lands on the restaurant the link names (rid=) when this login
    may see it, and on none otherwise — never on whichever location the
    session happens to be on. A link without rid (sent before links carried
    one) is the session's location. An open never starts an episode
    (rec_ledger.NON_OPENING): a key nobody was shown records nothing.
    Never raises."""
    try:
        rec = str((args or {}).get("rec") or "").strip()[:160]
        if not rec or not user:
            return False
        rid = user["restaurant_id"]
        raw_rid = str((args or {}).get("rid") or "").strip()
        if raw_rid:
            try:
                rid = int(raw_rid)
            except ValueError:
                return False
            if not _may_open_for(user, rid):
                return False
        import rec_ledger
        src = str((args or {}).get("src") or "").strip()
        surface = src if src in rec_ledger.SURFACES else "unknown"
        uid = user.get("id")
        return rec_ledger.record(rid, rec, "opened", surface=surface, user_id=uid,
                                 role=user.get("role"), meta={"via": "link"},
                                 source_ref=open_source_ref(rid, surface, uid))
    except Exception as e:
        print(f"[rec_delivery] link open not recorded: {e}")
        return False


def dashboard_url(rec=None, src=None, path="/", rid=None) -> str:
    """The dashboard (or a path on it), naming the recommendation."""
    import config
    return link(f"{config.base_url()}{path}", rec, src, rid)
