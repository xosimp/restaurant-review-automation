"""
rec_ledger.py — one identity and one event trail for every recommendation.

A recommendation used to live separately on every surface that said it:
Home kept `home_dismissals`, the schedule kept `schedule_recommendation_events`,
Ask kept `ask_cavnar_actions`, the brief and the alerts kept nothing. So an
owner's "no" on Home did not reach the brief or the alert saying the same
thing, the same news arrived five ways, and nobody could say how often a
recommendation was shown, let alone acted on.

Here a recommendation is an EPISODE: (restaurant, key) from the first time
any surface shows it until the owner answers it or it goes stale. Keys carry
their subject ("trim_day:Monday", "reprice:Carbonara") so a different day or
dish is a different recommendation. Every surface that shows one calls
`present()`; every answer calls `record()`; every surface that is about to
say something asks `silenced()` first. The admin console reads the trail
(admin_ops.recommendation_acceptance); the owner reads their own record of
it through rec_learning (what they followed, what it did, the timeline),
and the rankers read what it learned (rec_learning.effectiveness).

Events:  shown · opened · evidence_viewed · accepted · dismissed · snoozed ·
         completed · implemented · outcome · abandoned · checkin ·
         superseded · expired
Status:  open → accepted | completed | implemented | dismissed | expired |
         superseded  (snoozed stays open with snoozed_until; an accepted
         episode moves on to implemented when the change is actually made)

  accepted     the owner said yes (Track, a confirm, a hand-off)
  completed    the owner said Done
  implemented  the change was actually MADE, seen where it happens: a
               price applied, an order sent, a reply posted, a campaign
               sent, a schedule edited to match — `implemented_at` is set
               whatever the status, and the status moves to implemented
               from open, expired or accepted (Done stays Done)
  superseded   Cavnar replaced the recommendation before it was answered:
               the same key re-presented with materially different content
               (a new dollar figure or target), or a newer key of a kind
               that only ever has one live recommendation. Neither answered
               nor ignored — the newer episode carries on
  abandoned    the tracker measuring it was stopped (outcomes.abandon)
  checkin      the owner's "did you make this change / did anything else
               change" answer (meta documented at checkin())

Each episode is stored with the subject tags of its key (tags_for) so
success can be read per weekday / weekend, daypart, dish, item, review
category and topic, not only per kind; with the tracker that measures it
(`tracker_id`, permanent); and with the figure it was shown with
(`dollar_value`, every `shown` event's meta too) so predictions can be
calibrated against what was measured.

Writes are small and bounded: a `shown` is one row per episode, surface and
day however many times a page renders. Pure SQL; no model, no network.
"""
import json
import uuid
from datetime import datetime, timedelta

import models as _models_mod
from models import DB_PATH

EVENTS = ("shown", "opened", "evidence_viewed", "accepted", "dismissed", "snoozed",
          "completed", "outcome", "expired", "implemented", "superseded", "checkin", "abandoned")
TERMINAL = {"accepted": "accepted", "completed": "completed", "dismissed": "dismissed", "expired": "expired"}
# Events only the server writes: a client may not post them to /recs/event.
SERVER_ONLY_EVENTS = ("shown", "expired", "outcome", "implemented", "superseded", "checkin", "abandoned")
# Statuses an owner's answer can leave an episode in (taken or declined).
TAKEN_STATUSES = ("accepted", "completed", "implemented")
# Why an owner said no — one tap, every surface the same six. Stored on the
# answer's meta as `reason_code` (a free `reason` may ride beside it); an
# unknown code is refused at the door (400), never stored.
REASON_CODES = ("already_doing", "doesnt_fit", "too_costly", "bad_timing", "dont_trust_data", "other")
REASON_LABELS = {"already_doing": "already doing it", "doesnt_fit": "doesn't fit us", "too_costly": "too costly",
                 "bad_timing": "bad timing", "dont_trust_data": "don't trust the data", "other": "other"}
# A re-showing supersedes the open episode only when its content moved this
# much — the figure by at least SUPERSEDE_MIN_DOLLARS and SUPERSEDE_DOLLAR_SHARE
# of the old one, or the target changed — and only once the episode is this
# old, so two surfaces that price the same card a little differently on one
# day cannot flip it back and forth.
SUPERSEDE_MIN_DOLLARS = 25.0
SUPERSEDE_DOLLAR_SHARE = 0.25
SUPERSEDE_MIN_AGE_HOURS = 20
# Kinds with only ever one live recommendation per restaurant: a newer key of
# the kind replaces every other open one (a new labor target, a new Improve
# with Cavnar proposal).
REPLACING_KINDS = ("schedule_to_target", "optimizer")


def reason_label(code) -> str:
    return REASON_LABELS.get(code, "")
MODULES = ("reviews", "labor", "schedule", "food", "marketing", "intel", "guests", "ops", "home", "ask")
SURFACES = ("home", "brief_email", "brief_push", "weekly_email", "alert_sms", "alert_email", "alert_push",
            "queue", "ask", "schedule_review", "labor", "reviews", "food", "marketing", "intel", "issue_sms",
            "digest", "monthly_email", "ios", "web", "auto", "unknown", "dsr", "dsr_email")
# What each surface is called where a person reads it (the admin console's
# "by surface" breakdown). Every surface has one; a test holds them in step.
SURFACE_LABELS = {
    "home": "home", "brief_email": "brief email", "brief_push": "brief push", "weekly_email": "weekly email",
    "alert_sms": "alert text", "alert_email": "alert email", "alert_push": "alert push", "queue": "queue",
    "ask": "ask", "schedule_review": "schedule review", "labor": "labor", "reviews": "reviews", "food": "food",
    "marketing": "marketing", "intel": "intel", "issue_sms": "issue text", "digest": "digest",
    "monthly_email": "monthly email", "ios": "ios", "web": "web", "auto": "automatic", "unknown": "unknown",
    "dsr": "daily report", "dsr_email": "daily report email",
}


def surface_label(surface) -> str:
    return SURFACE_LABELS.get(surface, str(surface or "unknown"))

# How long each answer silences the same key everywhere.
SILENCE_DAYS = {"hide": 14, "not_for_us": 3650, "done": 3650}
# An accepted or completed recommendation is not re-asked while its outcome
# is being measured.
ACCEPTED_QUIET_DAYS = 14
# An open episode nobody has answered in this long is ignored, not pending:
# it closes as `expired`, and the next showing starts a new episode.
EXPIRE_AFTER_DAYS = 14
# A change made where it happens (a price applied, a post published) is
# recorded as `implemented` on the key's episode only while that episode is
# live or accepted, or ended within this many days (_may_implement).
IMPLEMENT_ATTACH_DAYS = 28
# Events that describe an episode someone already saw and never begin one:
# a tracker's verdict, an alert opened, the evidence read. An outcome with
# no episode behind it is an episode nobody was shown (H-5).
NON_OPENING = ("outcome", "opened", "evidence_viewed", "implemented", "superseded", "checkin", "abandoned")
# Keys the ledger holds as bookkeeping rather than as advice an owner was
# shown: the moment a quiet kind was restored (decisions.restore_kind), the
# quality weights applied, an on-call ask sent. They stay in the trail —
# restore_kind's stamp is what decisions.quiet_kinds counts from — and stay
# out of every acceptance figure (admin_ops), so none is read as a
# recommendation that was taken.
BOOKKEEPING_PREFIXES = ("restore_kind:", "calibration:", "standby:")
# How close (seconds) an answer the ledger already holds must be to an older
# ledger's row for sync_existing to treat the row as that same answer.
SAME_ANSWER_SECONDS = 300


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_rec_ledger(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS rec_instances (
            rec_id            TEXT PRIMARY KEY,
            restaurant_id     INTEGER NOT NULL,
            key               TEXT NOT NULL,
            module            TEXT,
            kind              TEXT,
            title             TEXT,
            status            TEXT NOT NULL DEFAULT 'open',
            dollar_value      REAL,
            confidence_band   TEXT,
            evidence_sources  TEXT,
            cross_module      INTEGER DEFAULT 0,
            model_written     INTEGER DEFAULT 0,
            cavnar_completes  INTEGER DEFAULT 0,
            expected_metric   TEXT,
            expected_by       TEXT,
            first_surface     TEXT,
            first_position    INTEGER,
            silenced_until    TEXT,
            snoozed_until     TEXT,
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            last_event_at     TEXT NOT NULL DEFAULT (datetime('now')),
            closed_at         TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_inst_key ON rec_instances(restaurant_id, key, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_inst_open ON rec_instances(status, last_event_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_inst_created ON rec_instances(created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS rec_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            rec_id         TEXT NOT NULL,
            restaurant_id  INTEGER NOT NULL,
            key            TEXT NOT NULL,
            event          TEXT NOT NULL,
            surface        TEXT,
            user_id        INTEGER,
            role           TEXT,
            dedupe         TEXT NOT NULL,
            meta           TEXT,
            at             TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(rec_id, dedupe)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_ev_rec ON rec_events(rec_id, event)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_ev_at ON rec_events(at)")
        # Every answer's idempotency check (_record_on, recorded,
        # sync_existing's "already carried") asks restaurant + key + dedupe;
        # decisions.history, shown_elsewhere_today and the timeline read one
        # restaurant's events of one kind by time. Without these both were a
        # scan of every restaurant's trail (re-audit B21).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_ev_rest_key_dedupe ON rec_events(restaurant_id, key, dedupe)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_ev_rest_event_at ON rec_events(restaurant_id, event, at)")
        # Columns added after the table shipped (ROI audit #17, #27, #36,
        # #37, and the owner view's redaction) — at boot, never on a call.
        have = {r[1] for r in conn.execute("PRAGMA table_info(rec_instances)").fetchall()}
        for col, decl in _ADDED_COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE rec_instances ADD COLUMN {col} {decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_inst_tracker ON rec_instances(tracker_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_inst_rest_created ON rec_instances(restaurant_id, created_at)")
        # Problems that surfaced — an alert, an issue, a close-out line —
        # with no recommendation covering their subject in the days before
        # (ROI audit #44). Admin-only; one row per restaurant, source,
        # subject and day.
        conn.execute("""CREATE TABLE IF NOT EXISTS rec_missed_detections (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL,
            source         TEXT NOT NULL,
            subject_key    TEXT NOT NULL,
            module         TEXT,
            detail         TEXT,
            lookback_days  INTEGER NOT NULL,
            day            TEXT NOT NULL,
            detected_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, source, subject_key, day)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_missed_at ON rec_missed_detections(detected_at)")
        conn.commit()
    finally:
        conn.close()


_ADDED_COLUMNS = (
    ("tags", "TEXT"),                 # JSON list from tags_for(), at present time
    ("implemented_at", "TEXT"),       # when the change was actually made
    ("tracker_id", "INTEGER"),        # recommendation_outcomes.id measuring it
    ("owner_only", "INTEGER DEFAULT 0"),   # rests on owner-only figures (DSR cites)
    ("superseded_by", "TEXT"),        # the rec_id that replaced it
    ("target", "TEXT"),               # the target it was shown with (a price, a %)
    # When the recommendation this episode carries on was first shown: an
    # episode that replaced an unanswered one (superseded) keeps the chain's
    # start, and the 14-day expiry runs from it (is_stale). NULL = its own
    # created_at. Without it a card re-priced every day never expired and was
    # never counted as ignored (re-audit B11).
    ("chain_started_at", "TEXT"),
    # The confidence the owner was shown, snapshotted at delivery (contract
    # K3, confidence audit): the overall % and each dimension, so what was
    # said can later be scored against what happened (admin calibration).
    # NULL confidence_pct = no confidence was shown with it (a surface that
    # carries none); accuracy/freshness are still recorded for calibration.
    ("confidence_pct", "INTEGER"),
    ("evidence_pct", "INTEGER"),
    ("accuracy_pct", "INTEGER"),
    ("accuracy_n", "INTEGER"),
    ("freshness_pct", "INTEGER"),
    ("freshness_as_of", "TEXT"),
    ("trust_version", "INTEGER"),
)

# rec_instances' snapshot columns, in confidence_engine.snapshot's order.
SNAPSHOT_COLS = ("confidence_pct", "evidence_pct", "accuracy_pct", "accuracy_n", "freshness_pct",
                 "freshness_as_of", "trust_version")
# A showing whose overall confidence moved this many points from the
# episode's last showing is logged in its `shown` meta (confidence_moved).
CONFIDENCE_MOVE_POINTS = 10


# ── keys ─────────────────────────────────────────────────────────────────────

def rec_key(kind: str, subject=None) -> str:
    """"kind:subject" — the identity every surface uses for one recommendation."""
    kind = str(kind or "").strip()
    subject = str(subject).strip() if subject not in (None, "") else ""
    return (f"{kind}:{subject}" if subject else kind)[:160]


def kind_of(key: str) -> str:
    return (str(key or "").split(":", 1)[0] or "unknown")[:60]


# ── subject tags (ROI audit #17) ─────────────────────────────────────────────
#
# A key's subject says what a recommendation is ABOUT — "trim_day:Saturday",
# "reprice:Carbonara", "top_issue:service" — but every rate grouped by kind,
# so "weekend staffing has worked best here" could not be said. tags_for()
# reads the subject into a small fixed vocabulary, stored on the episode when
# it is first shown and backfilled for older ones (backfill_tags):
#
#   day:<weekday>        a weekday named in the subject, or the weekday of an
#                        ISO date in it ("standby:2026-09-26:ana" → Saturday)
#   daytype:weekend|weekday   Friday–Sunday are the weekend: in a restaurant
#                        Friday night is a weekend shift
#   daypart:<part>       breakfast, brunch, lunch, dinner, late_night,
#                        happy_hour — when the subject names one
#   topic:<topic>        what kind of lever it pulls (staffing, hours,
#                        overtime, pricing, waste, ordering, purchasing,
#                        food_cost, replies, guest_experience, posting,
#                        guest_outreach, marketing, competition, sales,
#                        training, visibility)
#   focus:<daytype|daypart>_<topic>   the two together — "focus:weekend_staffing"
#   dish:<name>          a menu item (reprice)
#   item:<name>          an ingredient or stock item (waste, stock, price)
#   category:<review category>   a review theme (analyser.CATEGORIES)
#   food_category:<c>    the ingredient's own category (ingredients.category),
#                        looked up when the episode is created
#
# The module is its own column, not a tag. Tags are ids, never prose; labels
# come from tag_label().

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKEND_DAYS = ("friday", "saturday", "sunday")
# Longest phrase first: a matched phrase is consumed, so "late night" is
# never also "night". Morning / night are the schedule's own dayparts
# (shift_quality.daypart_of); the rest are the review analyser's.
DAYPARTS = (("late night", "late_night"), ("late-night", "late_night"), ("happy hour", "happy_hour"),
            ("breakfast", "breakfast"), ("brunch", "brunch"), ("lunch", "lunch"), ("dinner", "dinner"),
            ("morning", "morning"), ("night", "night"))
# Kinds whose ISO date is a PERIOD (a pay period's start, a week, the day a
# scan ran), not the day the recommendation is about — no weekday tag.
PERIOD_DATE_KINDS = ("labor_over", "labor", "stock", "checklist", "plan", "loss", "invoice")
# kind (or "dsr_action:<action kind>", "link:<link kind>") -> topic
KIND_TOPIC = {
    "trim_day": "staffing", "schedule_to_target": "staffing", "coverage": "staffing", "standby": "staffing",
    "suggested_pair": "staffing", "optimizer": "staffing", "cover": "staffing", "callout": "staffing",
    "schedule": "staffing", "schedule_unsent": "staffing", "shift_request": "staffing", "time_off": "staffing",
    "overtime": "overtime", "overtime_move": "overtime", "labor_over": "hours", "labor": "hours",
    "intraday_pulse": "hours", "pulse_cut": "hours",
    "reprice": "pricing", "price_spike": "purchasing", "invoice": "purchasing", "food_cost_driver": "food_cost",
    "cut_waste": "waste", "food_waste": "waste", "stock_low": "ordering", "critical_low": "ordering",
    "stock": "ordering", "diag_food": "food_cost", "food_diagnosis": "food_cost", "insight_food": "food_cost",
    "top_issue": "guest_experience", "diag_review": "guest_experience", "insight_review": "guest_experience",
    "rating": "guest_experience", "neg_spike": "guest_experience", "negative_trend": "guest_experience",
    "rating_threshold": "guest_experience", "rating_drop": "guest_experience",
    "publish_drafts": "replies", "urgent_reviews": "replies", "no_response": "replies",
    "stale_low_reviews": "replies", "review": "replies", "review_edit": "replies",
    "post_this_week": "posting", "first_post": "posting", "winback": "guest_outreach",
    "slow_day": "guest_outreach", "quiet_night": "guest_outreach", "review_requests": "guest_outreach",
    "insight_marketing": "marketing", "intel_recs": "competition", "insight_intel": "competition",
    "competitor_move": "competition", "ai_visibility_drop": "visibility", "loss": "loss",
    "dsr_action:adjust_staffing": "staffing", "dsr_action:control_hours": "hours",
    "dsr_action:coach_team": "training", "dsr_action:reorder": "ordering", "dsr_action:reduce_waste": "waste",
    "dsr_action:adjust_pricing": "pricing", "dsr_action:push_sales": "sales",
    "dsr_action:respond_reviews": "replies", "dsr_action:promote": "marketing",
    "link:reviews_x_labor": "staffing", "link:reviews_x_food_cost": "food_cost",
    "link:reviews_x_menu": "guest_experience", "link:marketing_x_reviews": "marketing",
    "link:intel_x_reviews": "competition",
}
# Kinds whose subject names a dish, and kinds whose subject names an item.
DISH_KINDS = ("reprice",)
ITEM_KINDS = ("cut_waste", "stock_low", "critical_low", "price_spike", "diag_food")
REVIEW_CATEGORY_KINDS = ("top_issue", "diag_review")
_TOPIC_LABELS = {"food_cost": "food cost", "guest_experience": "guest experience", "guest_outreach": "guest outreach"}
_DAYPART_LABELS = {"late_night": "late night", "happy_hour": "happy hour"}
MAX_TAGS = 10


def _topic_of(kind, key):
    parts = str(key or "").split(":")
    if kind in ("dsr_action", "link"):
        return KIND_TOPIC.get(f"{kind}:{parts[1]}" if len(parts) > 1 else kind)
    if kind.startswith("schedule_"):
        return "staffing"
    return KIND_TOPIC.get(kind)


def _slug(text, n=60) -> str:
    import re
    return re.sub(r"\s+", " ", str(text or "").strip().lower())[:n]


def _review_categories():
    try:
        from analyser import CATEGORIES
        return tuple(CATEGORIES)
    except Exception:
        return ()


def _names_a_thing(kind, bits) -> bool:
    """Whether the subject is a dish or an item's NAME (reprice:Friday Fish
    Fry, cut_waste:Dinner Rolls, stock_low:Sunday Gravy, a DSR action on
    food/<item>) — words in a name are not the day or daypart the
    recommendation is about (re-audit B14)."""
    if kind in DISH_KINDS or kind in ITEM_KINDS:
        return True
    return kind == "dsr_action" and len(bits) > 1 and bits[1].partition("/")[0] == "food" and "/" in bits[1]


def tags_for(key, module=None, kind=None, food_category=None) -> list:
    """The subject tags of one recommendation key (the vocabulary above),
    sorted. Pure — `food_category` is looked up by the caller. A subject
    that is an opaque hash (a model read's line) carries only its topic; a
    subject that is a dish or item name carries no day, day type or daypart
    read out of the name."""
    import re
    key = str(key or "").strip()
    kind = (kind or kind_of(key)).strip()
    subject = key.split(":", 1)[1] if ":" in key else ""
    low = subject.lower().replace("_", " ")
    named = _names_a_thing(kind, subject.split(":"))
    if named:
        low = ""                      # nothing about WHEN is read from a name
    tags = set()
    days = [d for d in WEEKDAYS if re.search(rf"(?<![a-z]){d}s?(?![a-z])", low)]
    if kind not in PERIOD_DATE_KINDS and not named:
        for iso in re.findall(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", subject):
            try:
                days.append(WEEKDAYS[datetime.strptime(iso, "%Y-%m-%d").weekday()])
            except ValueError:
                pass
    days = list(dict.fromkeys(days))
    for d in days:
        tags.add(f"day:{d}")
    daytypes = {("weekend" if d in WEEKEND_DAYS else "weekday") for d in days}
    if re.search(r"(?<![a-z])weekends?(?![a-z])", low):
        daytypes.add("weekend")
    for t in daytypes:
        tags.add(f"daytype:{t}")
    parts, rest = [], low
    for word, part in DAYPARTS:
        pat = rf"(?<![a-z]){re.escape(word)}s?(?![a-z])"
        if re.search(pat, rest):
            rest = re.sub(pat, " ", rest)
            if part not in parts:
                parts.append(part)
    for p in parts:
        tags.add(f"daypart:{p}")
    topic = _topic_of(kind, key)
    if topic:
        tags.add(f"topic:{topic}")
        for t in sorted(daytypes):
            tags.add(f"focus:{t}_{topic}")
        for p in parts:
            tags.add(f"focus:{p}_{topic}")
    bits = subject.split(":")
    first = bits[0].strip()
    if kind in DISH_KINDS and first:
        tags.add(f"dish:{_slug(first)}")
    if kind in ITEM_KINDS and first and len(first.split()) <= 4:
        tags.add(f"item:{_slug(first)}")
    if kind == "dsr_action" and len(bits) > 1 and "/" in bits[1]:
        block, _, entity = bits[1].partition("/")
        if block == "food" and entity.strip():
            tags.add(f"item:{_slug(entity.replace('-', ' '))}")
    cats = _review_categories()
    cat = first.lower().replace(" ", "_")          # "Wait time" is wait_time
    if kind in REVIEW_CATEGORY_KINDS and cat in cats:
        tags.add(f"category:{cat}")
    if kind == "link" and len(bits) > 1 and bits[1].strip().lower() in cats:
        tags.add(f"category:{bits[1].strip().lower()}")
    if food_category:
        tags.add(f"food_category:{_slug(food_category, 40)}")
    if len(tags) > MAX_TAGS:
        # A cap never drops the topic, the focus or the day type.
        tags = sorted(tags, key=lambda t: (not t.startswith(("topic:", "focus:", "daytype:")), t))[:MAX_TAGS]
    return sorted(tags)


def tag_label(tag) -> str:
    """How a tag reads to an owner: "focus:weekend_staffing" → "Weekend
    staffing", "day:saturday" → "Saturday", "category:wait_time" → "Wait time"."""
    tag = str(tag or "")
    head, _, val = tag.partition(":")
    if head == "day":
        return val.capitalize()
    if head == "daytype":
        return "Weekends" if val == "weekend" else "Weekdays"
    if head == "daypart":
        return _DAYPART_LABELS.get(val, val).capitalize()
    if head == "topic":
        return _TOPIC_LABELS.get(val, val.replace("_", " ")).capitalize()
    if head == "focus":
        when, _, topic = val.partition("_")
        if when == "late" or when == "happy":            # late_night_<topic>, happy_hour_<topic>
            rest = topic.split("_", 1)
            when, topic = f"{when}_{rest[0]}", (rest[1] if len(rest) > 1 else "")
        return (f"{_DAYPART_LABELS.get(when, when).capitalize()} "
                f"{_TOPIC_LABELS.get(topic, topic.replace('_', ' '))}").strip()
    if head == "category":
        try:
            from analyser import category_label
            return category_label(val).capitalize()
        except Exception:
            return val.replace("_", " ").capitalize()
    if head in ("dish", "item", "food_category"):
        return val.capitalize()
    return tag


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _stamp(v):
    """A stored timestamp as the ledger's own 'YYYY-MM-DD HH:MM:SS' (UTC),
    or None. Accepts sqlite datetime('now') text, isoformat and datetimes;
    an aware value is converted to UTC."""
    if not v:
        return None
    if isinstance(v, datetime):
        d = v
    else:
        t = str(v).strip().replace("T", " ").replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(t)
        except ValueError:
            try:
                d = datetime.strptime(t[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if d.tzinfo is not None:
        from datetime import timezone as _tz
        d = d.astimezone(_tz.utc).replace(tzinfo=None)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def counts_in_acceptance(key) -> bool:
    """Whether an episode under this key is a recommendation the acceptance
    figures count (BOOKKEEPING_PREFIXES are not)."""
    return not str(key or "").startswith(BOOKKEEPING_PREFIXES)


def stale_cutoff(days=EXPIRE_AFTER_DAYS, now=None) -> str:
    """Episodes created before this and still unanswered are ignored."""
    return ((now or datetime.utcnow()) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _col(row, name):
    """row[name], or None for a row read without that column."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def chain_start(row):
    """When the recommendation an episode carries was first shown: the
    chain's start for an episode that replaced an unanswered one, else its
    own creation."""
    return _col(row, "chain_started_at") or _col(row, "created_at")


def is_stale(row, now=None, days=EXPIRE_AFTER_DAYS) -> bool:
    """THE expiry rule, read the same way by _open_or_new, expire_stale and
    the admin page's `ignored`: an episode still open (no accept, complete
    or dismiss) that was CREATED more than `days` ago and is not inside a
    snooze. Measured from creation, not from the last event: a card shown
    every day bumps its last event daily, so it never expired and the
    recommendations owners ignore most were never counted as ignored. An
    episode that superseded an unanswered one is measured from the chain's
    start (chain_start): re-pricing a card must not restart the clock."""
    if row is None or row["status"] != "open":
        return False
    now_s = (now or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    if row["snoozed_until"] and row["snoozed_until"] > now_s:
        return False
    return (chain_start(row) or "") < stale_cutoff(days, now)


def _latest(conn, rid, key):
    return conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND key=? ORDER BY created_at DESC, rowid DESC "
                        "LIMIT 1", (rid, key)).fetchone()


def _episode_at(conn, rid, key, at):
    """The episode that was current at `at`: the latest created at or before
    it. An answer belongs to what the owner was looking at when they gave
    it, never to an episode shown afterwards."""
    return conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND key=? AND created_at <= ? "
                        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (rid, key, at)).fetchone()


def _silenced_row(row, now=None) -> bool:
    if row is None:
        return False
    now = now or _now()
    if row["silenced_until"] and row["silenced_until"] > now:
        return True
    if row["status"] == "open" and row["snoozed_until"] and row["snoozed_until"] > now:
        return True
    return False


def _food_category(conn, rid, key, kind):
    """The ingredient's own category for an item-subject key, else None."""
    if kind not in ITEM_KINDS or ":" not in str(key):
        return None
    item = str(key).split(":", 1)[1].split(":", 1)[0].strip()
    if not item:
        return None
    try:
        row = conn.execute("SELECT category FROM ingredients WHERE restaurant_id=? AND lower(name)=lower(?) "
                           "AND category IS NOT NULL AND category != '' LIMIT 1", (rid, item)).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def _stored_tags(conn, rid, key, module, kind):
    kind = kind or kind_of(key)
    return json.dumps(tags_for(key, module, kind, food_category=_food_category(conn, rid, key, kind)))


def _material_change(row, attrs, now=None):
    """Why re-showing this open episode with `attrs` is a different
    recommendation (None when it is the same one): its dollar figure or its
    target moved (SUPERSEDE_* above), and the episode is old enough that two
    surfaces pricing it slightly differently on one day cannot flip it."""
    attrs = attrs or {}
    now = now or datetime.utcnow()
    made = _stamp(row["created_at"])
    if not made or made > (now - timedelta(hours=SUPERSEDE_MIN_AGE_HOURS)).strftime("%Y-%m-%d %H:%M:%S"):
        return None
    old, new = row["dollar_value"], _num(attrs.get("dollar_value"))
    if old is not None and new is not None:
        if abs(new - old) >= max(SUPERSEDE_MIN_DOLLARS, SUPERSEDE_DOLLAR_SHARE * abs(old)):
            return {"why": "figure_changed", "from": old, "to": new}
    try:
        old_t = row["target"]
    except (IndexError, KeyError):
        old_t = None
    new_t = attrs.get("target")
    if old_t not in (None, "") and new_t not in (None, "") and str(new_t)[:60] != str(old_t):
        return {"why": "target_changed", "from": old_t, "to": str(new_t)[:60]}
    return None


def _supersede(conn, old_rec_id, rid, key, by=None, meta=None):
    """Close an OPEN episode as superseded — Cavnar replaced it; nobody
    answered or ignored it. Returns whether it was closed."""
    n = conn.execute("UPDATE rec_instances SET status='superseded', closed_at=?, superseded_by=? "
                     "WHERE rec_id=? AND status='open'", (_now(), by, old_rec_id)).rowcount
    if n:
        _add_event(conn, old_rec_id, rid, key, "superseded", dedupe="superseded",
                   meta=dict(meta or {}, by=by))
    return bool(n)


def _fill_missing(conn, row, attrs, title=None):
    """An open episode first shown without a figure, a target or a metric
    (a brief line, a record()-opened episode) takes them from the first
    showing that has them — the prediction a result is calibrated against
    was otherwise never learned (re-audit B16). Never overwrites a value it
    has (a different one is _material_change's to judge); owner_only only
    ever tightens."""
    attrs = attrs or {}
    sets, args = [], []
    if _col(row, "dollar_value") is None and _num(attrs.get("dollar_value")) is not None:
        sets.append("dollar_value=?")
        args.append(_num(attrs["dollar_value"]))
    if _col(row, "target") in (None, "") and attrs.get("target") not in (None, ""):
        sets.append("target=?")
        args.append(str(attrs["target"])[:60])
    if not _col(row, "expected_metric") and attrs.get("expected_metric"):
        sets.append("expected_metric=?")
        args.append(str(attrs["expected_metric"])[:60])
    if not _col(row, "title") and title:
        sets.append("title=?")
        args.append(str(title)[:200])
    if attrs.get("owner_only") and not _col(row, "owner_only"):
        sets.append("owner_only=1")
    if not _col(row, "confidence_band") and attrs.get("confidence_band"):
        sets.append("confidence_band=?")
        args.append(str(attrs["confidence_band"])[:20])
    # The confidence snapshot is taken at the first showing that has one
    # and never overwritten: calibration scores what was said when it was
    # first said (K3). A showing without an overall % (accuracy and
    # freshness only) never blocks a later one that has it.
    snap = attrs.get("_snapshot") or {}
    if snap and _col(row, "confidence_pct") is None and (
            snap.get("confidence_pct") is not None or all(_col(row, c) is None for c in SNAPSHOT_COLS)):
        for c in SNAPSHOT_COLS:
            if snap.get(c) is not None:
                sets.append(f"{c}=?")
                args.append(snap[c])
    if sets:
        conn.execute(f"UPDATE rec_instances SET {', '.join(sets)} WHERE rec_id=?", (*args, row["rec_id"]))


def _open_or_new(conn, rid, key, module, kind, title=None, attrs=None, surface=None, position=None, created_at=None):
    """The episode a showing belongs to: the open one, else a new one — unless
    the latest was answered and is still silencing, or is open inside a
    snooze ("Not today" — the same rule silenced() reads), then None. An
    open episode re-shown with materially different content
    (_material_change) is closed as superseded and a new one begins; a new
    key of a REPLACING_KINDS kind supersedes the kind's other open keys. A
    replacement carries on the chain it replaced (chain_started_at), so the
    expiry clock is not restarted by a new figure; a replaced episode that
    was already stale expires instead."""
    row = _latest(conn, rid, key)
    now = _now()
    replaced = None
    chain = None
    if row is not None:
        if row["status"] == "open":
            if not is_stale(row):
                if row["snoozed_until"] and row["snoozed_until"] > now:
                    return None
                change = _material_change(row, attrs) if created_at is None else None
                if not change:
                    _fill_missing(conn, row, attrs, title)
                    return row["rec_id"]
                replaced = (row["rec_id"], change)
                chain = chain_start(row)
            else:
                _close(conn, row["rec_id"], rid, key, "expired", meta={"reason": "no answer"})
        elif _silenced_row(row, now):
            return None
    attrs = attrs or {}
    kind = kind or kind_of(key)
    others = []
    if kind in REPLACING_KINDS and created_at is None:
        for other in conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND kind=? AND status='open' "
                                  "AND key != ?", (rid, kind, key)).fetchall():
            if is_stale(other):
                _close(conn, other["rec_id"], rid, other["key"], "expired", meta={"reason": "no answer"})
                continue
            others.append(other)
            start = chain_start(other)
            if start and (chain is None or start < chain):
                chain = start
    rec_id = uuid.uuid4().hex
    snap = attrs.get("_snapshot") or {}
    conn.execute(
        "INSERT INTO rec_instances (rec_id, restaurant_id, key, module, kind, title, dollar_value, confidence_band, "
        "evidence_sources, cross_module, model_written, cavnar_completes, expected_metric, expected_by, first_surface, "
        "first_position, created_at, last_event_at, tags, owner_only, target, chain_started_at, "
        + ", ".join(SNAPSHOT_COLS) + ") "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?," + ",".join("?" for _ in SNAPSHOT_COLS) + ")",
        (rec_id, rid, key, module, kind, (title or "")[:200] or None,
         _num(attrs.get("dollar_value")), attrs.get("confidence_band"),
         json.dumps(sorted(set(attrs.get("evidence_sources") or []))) if attrs.get("evidence_sources") else None,
         1 if (attrs.get("cross_module") or len(set(attrs.get("evidence_sources") or [])) > 1) else 0,
         1 if attrs.get("model_written") else 0, 1 if attrs.get("cavnar_completes") else 0,
         attrs.get("expected_metric"), attrs.get("expected_by"), surface, position, created_at or now,
         created_at or now, _stored_tags(conn, rid, key, module, kind), 1 if attrs.get("owner_only") else 0,
         str(attrs["target"])[:60] if attrs.get("target") not in (None, "") else None,
         chain if (chain and chain < (created_at or now)) else None,
         *(snap.get(c) for c in SNAPSHOT_COLS)))
    if replaced:
        _supersede(conn, replaced[0], rid, key, by=rec_id, meta=replaced[1])
    for other in others:
        _supersede(conn, other["rec_id"], rid, other["key"], by=rec_id, meta={"why": "replaced", "key": key})
    return rec_id


def _num(v):
    try:
        return round(float(v), 2) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _add_event(conn, rec_id, rid, key, event, surface=None, user_id=None, role=None, dedupe=None, meta=None, at=None):
    at = at or _now()
    cur = conn.execute(
        "INSERT OR IGNORE INTO rec_events (rec_id, restaurant_id, key, event, surface, user_id, role, dedupe, meta, at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rec_id, rid, key, event, surface, user_id, (role or None), dedupe or f"{event}:{uuid.uuid4().hex}",
         json.dumps(meta)[:2000] if meta else None, at))
    if cur.rowcount:
        conn.execute("UPDATE rec_instances SET last_event_at=MAX(last_event_at, ?) WHERE rec_id=?", (at, rec_id))
    return bool(cur.rowcount)


def _close(conn, rec_id, rid, key, status, meta=None, at=None):
    # An answer given to an episode that had already expired is still an
    # answer: the episode takes the answer's status. It stayed 'expired',
    # so the admin acceptance view and quiet_kinds counted an answered
    # recommendation as ignored (M-31, H-22). Expiry itself only ever
    # closes an open episode.
    # (A superseded episode answered late — a synced answer given while it
    # was current — takes the answer the same way.)
    allowed = "('open')" if status == "expired" else "('open', 'expired', 'superseded')"
    conn.execute(f"UPDATE rec_instances SET status=?, closed_at=? WHERE rec_id=? AND status IN {allowed}",
                 (status, at or _now(), rec_id))
    if status == "expired":
        _add_event(conn, rec_id, rid, key, "expired", dedupe="expired", meta=meta)


# ── the calls every surface makes ────────────────────────────────────────────

def present(restaurant_id, key, module, surface, title=None, kind=None, position=None, user_id=None,
            db_path=DB_PATH, **attrs):
    """A surface is showing this recommendation. Returns its rec_id, or None
    when the owner has already answered it (the caller should not show it).

    attrs: dollar_value, confidence_band, evidence_sources (list of modules),
    cross_module, model_written, cavnar_completes, expected_metric,
    expected_by, target (the price or % it asks for — a new one supersedes
    the open episode), owner_only (it rests on figures only an owner sees).
    One `shown` row per episode, surface and day."""
    out = present_many(restaurant_id, [dict(key=key, module=module, title=title, kind=kind, position=position,
                                            **attrs)], surface, user_id=user_id, db_path=db_path)
    return out.get(key)


_PRESENT_ATTRS = ("dollar_value", "confidence_band", "evidence_sources", "cross_module", "model_written",
                  "cavnar_completes", "expected_metric", "expected_by", "target", "owner_only")


def _snapshots(restaurant_id, items, db_path=DB_PATH) -> dict:
    """{index in items: snapshot fields} for a batch (contract K3). An item
    carrying its K1 `confidence` is snapshotted from it. One shown with none
    (a surface that carries no confidence yet) has its Historical Accuracy
    and Data Freshness measured here, once per batch through rec_trust, so
    calibration can still read them — its overall and evidence stay NULL:
    nothing was shown. Never raises."""
    out = {}
    try:
        import rec_trust
        import confidence_engine as ce
        import data_freshness
    except Exception as e:
        print(f"[rec_ledger] confidence snapshot unavailable: {e}")
        return out
    ctx = None
    for i, it in enumerate(items or []):
        conf = it.get("confidence")
        try:
            if isinstance(conf, dict) and conf.get("version"):
                out[i] = rec_trust.snapshot_fields(conf)
                continue
            if not str(it.get("key") or "").strip():
                continue
            if ctx is None:
                ctx = rec_trust.Context(restaurant_id, db_path=db_path)
            acc = ce.accuracy(ctx.record(kind_of(it.get("key"))))
            fr = ce.freshness(ctx.sources(data_freshness.sources_for(it.get("evidence_sources")
                                                                     or [it.get("module")])))
            out[i] = {"confidence_pct": None, "evidence_pct": None, "accuracy_pct": acc.get("pct"),
                      "accuracy_n": acc.get("n"), "freshness_pct": fr.get("pct"),
                      "freshness_as_of": fr.get("as_of_iso"), "trust_version": ce.VERSION}
        except Exception as e:
            print(f"[rec_ledger] confidence snapshot failed for {it.get('key')}: {e}")
    return out


def _last_shown_pct(conn, rec_id):
    """The overall confidence % the episode's latest showing carried, or None."""
    try:
        row = conn.execute("SELECT meta FROM rec_events WHERE rec_id=? AND event='shown' "
                           "ORDER BY at DESC, id DESC LIMIT 1", (rec_id,)).fetchone()
        return (json.loads(row["meta"] or "{}") or {}).get("confidence_pct") if row else None
    except Exception:
        return None


def local_day(conn, restaurant_id, now=None) -> str:
    """The restaurant's own calendar date (its timezone) as YYYY-MM-DD — the
    day a `shown` is counted once per surface. A UTC day began at 7pm CDT,
    so an evening Home view and the next morning's were one "day" and
    counted once, while every other "today" (decisions.shown_elsewhere_today,
    the brief) is local (re-audit B25). Falls back to operator time."""
    from datetime import timezone as _tz
    from time_utils import restaurant_tz
    try:
        row = conn.execute("SELECT timezone FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        name = row[0] if row else None
    except Exception:
        name = None
    utc = (now or datetime.utcnow()).replace(tzinfo=_tz.utc)
    return utc.astimezone(restaurant_tz(name or None)).strftime("%Y-%m-%d")


def present_many(restaurant_id, items: list, surface: str, user_id=None, db_path=DB_PATH, replaces=()) -> dict:
    """{key: rec_id or None} for a batch shown together (a Home page, a
    brief), on one connection. Never raises: measurement must not take a
    surface down.

    `replaces` names kinds this batch is the WHOLE current set of (a model
    read's numbered lines, keyed by a hash of their words): an open episode
    of one of those kinds that is not in the batch was replaced by the new
    read, and closes as superseded rather than lingering to expire as
    ignored."""
    out = {}
    if not restaurant_id or not items:
        return out
    # Measured before the write connection opens (the snapshot reads the
    # ledger and each source on connections of its own).
    snaps = _snapshots(restaurant_id, items, db_path=db_path)
    try:
        conn = get_conn(db_path)
    except Exception as e:
        print(f"[rec_ledger] present unavailable: {e}")
        return out
    try:
        day = local_day(conn, restaurant_id)
        for i, it in enumerate(items):
            key = str(it.get("key") or "").strip()[:160]
            if not key:
                continue
            attrs = {k: it.get(k) for k in _PRESENT_ATTRS}
            conf = it.get("confidence") if isinstance(it.get("confidence"), dict) else None
            if not attrs.get("confidence_band") and conf and conf.get("band"):
                attrs["confidence_band"] = conf["band"]
            snap = snaps.get(i) or {}
            attrs["_snapshot"] = snap
            pos = it.get("position") if it.get("position") is not None else i
            rec_id = _open_or_new(conn, restaurant_id, key, it.get("module"), it.get("kind"), it.get("title"), attrs,
                                  surface, pos)
            out[key] = rec_id
            if rec_id:
                meta = {"position": pos}
                # The figure it was shown with, each time (calibration, #43).
                if _num(attrs.get("dollar_value")) is not None:
                    meta["dollar_value"] = _num(attrs["dollar_value"])
                # ...and the confidence it was shown with, each time (K3),
                # noting a move since the episode's last showing (the
                # confidence change log, CA6 §C).
                for c in SNAPSHOT_COLS:
                    if snap.get(c) is not None:
                        meta[c] = snap[c]
                if snap.get("confidence_pct") is not None:
                    before = _last_shown_pct(conn, rec_id)
                    if before is not None and abs(int(snap["confidence_pct"]) - int(before)) >= CONFIDENCE_MOVE_POINTS:
                        meta["confidence_moved"] = {"from": int(before), "to": int(snap["confidence_pct"])}
                _add_event(conn, rec_id, restaurant_id, key, "shown", surface=surface, user_id=user_id,
                           dedupe=f"shown:{surface}:{day}", meta=meta)
        kinds = [str(k) for k in (replaces or ()) if k]
        if kinds:
            batch = [k for k in out]
            marks_k = ",".join("?" for _ in kinds)
            marks_b = ",".join("?" for _ in batch) or "''"
            for r in conn.execute(
                    f"SELECT rec_id, key FROM rec_instances WHERE restaurant_id=? AND status='open' "
                    f"AND kind IN ({marks_k}) AND key NOT IN ({marks_b})",
                    (restaurant_id, *kinds, *batch)).fetchall():
                _supersede(conn, r["rec_id"], restaurant_id, r["key"], meta={"why": "replaced", "surface": surface})
        conn.commit()
    except Exception as e:
        print(f"[rec_ledger] present failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    return out


def record(restaurant_id, key, event, surface=None, user_id=None, role=None, meta=None, source_ref=None,
           silence_days=None, snooze_until=None, at=None, silence_until=None, db_path=DB_PATH,
           rec_id=None, require_existing=False) -> bool:
    """The owner (or Cavnar on their behalf) did something with a
    recommendation. Terminal events close the episode; `dismissed` and
    `completed`/`accepted` silence the key everywhere for SILENCE_DAYS /
    ACCEPTED_QUIET_DAYS (or `silence_days`, or an absolute `silence_until`).
    Never raises.

    `rec_id` names the episode the answer belongs to (a check-in on the
    result a tracker measured — K1) instead of the key's latest; it must be
    an episode of this restaurant and key, else nothing is recorded.
    `require_existing` refuses to start an episode at the answer: a
    client-originated answer names a recommendation it was shown (K2).

    `source_ref` names the answer: the same answer recorded again (a sync,
    a retry) is a no-op for this key across EVERY episode, not only the
    latest — a nightly replay of an old answer used to land on each new
    episode and close it.

    `at` is when the answer was given, for an answer carried in from
    another table (sync_existing). It attaches to the episode that was
    current then, never to one shown afterwards, and its silence runs from
    `at`, not from the moment it was copied.

    An outcome, an open or an evidence view never starts an episode: with
    nothing shown behind it there is nothing to attach it to."""
    if event not in EVENTS:
        raise ValueError(f"unknown recommendation event {event}")
    key = str(key or "").strip()[:160]
    if not restaurant_id or not key:
        return False
    when = _stamp(at) if at else None
    if meta and "reason_code" in meta and meta.get("reason_code") not in REASON_CODES:
        # The routes refuse an unknown code with a 400; an internal caller's
        # is dropped rather than stored as if it were one of the six.
        meta = {k: v for k, v in meta.items() if k != "reason_code"}
    try:
        conn = get_conn(db_path)
    except Exception as e:
        print(f"[rec_ledger] record unavailable: {e}")
        return False
    try:
        added = _record_on(conn, restaurant_id, key, event, surface=surface, user_id=user_id, role=role, meta=meta,
                           source_ref=source_ref, silence_days=silence_days, snooze_until=snooze_until, when=when,
                           silence_until=silence_until, rec_id=rec_id, require_existing=require_existing)
        conn.commit()
        return added
    except Exception as e:
        print(f"[rec_ledger] record failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def _may_implement(row, when=None) -> bool:
    """Whether a change made now can be the one this episode recommended:
    it is live (open, not stale), accepted, or it ended — answered, expired,
    superseded — within IMPLEMENT_ATTACH_DAYS. A post today is not the
    change a "post this week" card nobody answered in March asked for
    (re-audit B15)."""
    now = datetime.strptime(when, "%Y-%m-%d %H:%M:%S") if when else datetime.utcnow()
    st = row["status"]
    if st == "accepted" or (st == "open" and not is_stale(row, now=now)):
        return True
    closed = _stamp(_col(row, "closed_at"))
    ended = datetime.strptime(closed, "%Y-%m-%d %H:%M:%S") if closed else None
    if st in ("open", "expired"):
        # An ignored episode ended when it went stale — EXPIRE_AFTER_DAYS
        # after its chain began — not when the 8am job got round to closing
        # it (a card from March closed this morning is not recent).
        start = _stamp(chain_start(row))
        stale_at = (datetime.strptime(start, "%Y-%m-%d %H:%M:%S") + timedelta(days=EXPIRE_AFTER_DAYS)) if start else None
        ended = min(x for x in (ended, stale_at) if x is not None) if (ended or stale_at) else None
    return ended is not None and ended >= now - timedelta(days=IMPLEMENT_ATTACH_DAYS)


def _record_on(conn, restaurant_id, key, event, surface=None, user_id=None, role=None, meta=None, source_ref=None,
               silence_days=None, snooze_until=None, when=None, silence_until=None, rec_id=None,
               require_existing=False) -> bool:
    """record()'s body on the caller's connection, uncommitted — for a
    caller already inside its own write transaction (a schedule save), where
    a second connection would wait on the lock that caller holds."""
    dedupe = f"{event}:{source_ref}" if source_ref else None
    if dedupe and conn.execute("SELECT 1 FROM rec_events WHERE restaurant_id=? AND key=? AND dedupe=? LIMIT 1",
                               (restaurant_id, key, dedupe)).fetchone():
        return False
    if rec_id:
        row = conn.execute("SELECT * FROM rec_instances WHERE rec_id=? AND restaurant_id=? AND key=?",
                           (rec_id, restaurant_id, key)).fetchone()
        if row is None:
            return False
    elif when:
        row = _episode_at(conn, restaurant_id, key, when)
        if row is None and _latest(conn, restaurant_id, key) is not None:
            # Every episode of this key began after the answer: whatever
            # it answered was never logged, and it cannot answer what
            # the owner has been shown since.
            return False
    else:
        row = _latest(conn, restaurant_id, key)
    if row is not None and event == "implemented" and not rec_id and not _may_implement(row, when):
        return False
    if row is None:
        if event in NON_OPENING or require_existing:
            return False
        # Answered before any surface logged showing it (an alert, an
        # older client): the episode starts at the answer.
        rec_id = _open_or_new(conn, restaurant_id, key, (meta or {}).get("module"), kind_of(key), surface=surface,
                              created_at=when)
        if rec_id is None:
            return False
    else:
        rec_id = row["rec_id"]
    added = _add_event(conn, rec_id, restaurant_id, key, event, surface=surface, user_id=user_id, role=role,
                       dedupe=dedupe, meta=meta, at=when)
    base = when or _now()
    if added:
        if event in TERMINAL:
            _close(conn, rec_id, restaurant_id, key, TERMINAL[event], at=when)
        if event == "dismissed":
            days = silence_days or SILENCE_DAYS.get((meta or {}).get("kind") or "hide", SILENCE_DAYS["hide"])
            conn.execute("UPDATE rec_instances SET silenced_until=COALESCE(?, datetime(?, ?)) WHERE rec_id=?",
                         (_stamp(silence_until), base, f"+{int(days)} days", rec_id))
        elif event in ("accepted", "completed"):
            conn.execute("UPDATE rec_instances SET silenced_until=MAX(COALESCE(silenced_until, ''), "
                         "COALESCE(?, datetime(?, ?))) WHERE rec_id=?",
                         (_stamp(silence_until), base, f"+{int(silence_days or ACCEPTED_QUIET_DAYS)} days", rec_id))
        elif event == "snoozed":
            until = snooze_until or (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            # "Not today" on a card the 8am job had just expired (it was
            # still on the owner's screen) is an answer to that card: it
            # reopens, snoozed, instead of staying expired and unsilenced so
            # the next surface showed it again at once (re-audit B17).
            conn.execute("UPDATE rec_instances SET snoozed_until=?, "
                         "status=CASE WHEN status='expired' THEN 'open' ELSE status END, "
                         "closed_at=CASE WHEN status='expired' THEN NULL ELSE closed_at END WHERE rec_id=?",
                         (str(until), rec_id))
        elif event == "implemented":
            # The change was made. Open, expired or accepted become
            # implemented; Done stays Done and a "not for us" stays
            # declined — the time is kept on every one of them.
            conn.execute(
                "UPDATE rec_instances SET implemented_at=COALESCE(implemented_at, ?), "
                "status=CASE WHEN status IN ('open','expired','accepted') THEN 'implemented' ELSE status END, "
                "closed_at=CASE WHEN status IN ('open','expired') THEN ? ELSE closed_at END, "
                "silenced_until=MAX(COALESCE(silenced_until, ''), COALESCE(?, datetime(?, ?))) WHERE rec_id=?",
                (base, base, _stamp(silence_until), base, f"+{int(silence_days or ACCEPTED_QUIET_DAYS)} days",
                 rec_id))
        tracker = (meta or {}).get("tracker_id") or (meta or {}).get("tracking")
        if isinstance(tracker, int) and not isinstance(tracker, bool) and tracker > 0:
            # The tracker measuring this episode, kept for good (#36).
            conn.execute("UPDATE rec_instances SET tracker_id=? WHERE rec_id=? AND tracker_id IS NULL",
                         (tracker, rec_id))
    return added


def implemented_on(conn, restaurant_id, keys, surface, user_id=None, source_ref=None, meta=None) -> int:
    """implemented() on the caller's connection and inside its transaction
    (the caller commits). Raises — the caller's savepoint decides."""
    if isinstance(keys, str):
        keys = [keys]
    n = 0
    for k in dict.fromkeys(str(k or "").strip()[:160] for k in (keys or [])):
        if k and _record_on(conn, restaurant_id, k, "implemented", surface=surface, user_id=user_id, meta=meta,
                            source_ref=source_ref):
            n += 1
    return n


def recorded(restaurant_id, key, event, source_ref, db_path=DB_PATH) -> bool:
    """Whether this answer (`event` under `source_ref`) is already in the
    trail for this key, on any episode. Never raises."""
    key = str(key or "").strip()[:160]
    if not restaurant_id or not key or not source_ref:
        return False
    try:
        conn = get_conn(db_path)
    except Exception:
        return False
    try:
        return bool(conn.execute("SELECT 1 FROM rec_events WHERE restaurant_id=? AND key=? AND dedupe=? LIMIT 1",
                                 (restaurant_id, key, f"{event}:{source_ref}")).fetchall())
    except Exception:
        return False
    finally:
        conn.close()


def implemented(restaurant_id, keys, surface, user_id=None, role=None, source_ref=None, meta=None,
                db_path=DB_PATH) -> int:
    """The change a recommendation asked for was actually MADE — a price
    applied, an order sent, a reply posted, a campaign sent, a schedule
    edited to match (ROI audit #27). Called where the change happens, never
    from a button that only says yes. `keys` is one key or several (a
    supplier order implements every stock line on it).

    Only an episode someone was shown can be implemented: a key with no
    episode is a change nobody recommended, and is not recorded. Idempotent
    per (key, source_ref). Returns how many keys were recorded. Never
    raises."""
    if isinstance(keys, str):
        keys = [keys]
    n = 0
    for k in dict.fromkeys(str(k or "").strip()[:160] for k in (keys or [])):
        if not k:
            continue
        try:
            if record(restaurant_id, k, "implemented", surface=surface, user_id=user_id, role=role,
                      meta=meta, source_ref=source_ref, db_path=db_path):
                n += 1
        except Exception as e:
            print(f"[rec_ledger] implemented not recorded for {k}: {e}")
    return n


def link_tracker(restaurant_id, key, tracker_id, db_path=DB_PATH) -> bool:
    """Tie a tracker (recommendation_outcomes.id) to the episode it measures
    — the latest episode of `key` — for good. A no-op when the episode is
    already linked or there is none. Never raises."""
    key = str(key or "").strip()[:160]
    try:
        tracker_id = int(tracker_id)
    except (TypeError, ValueError):
        return False
    if not restaurant_id or not key or tracker_id <= 0:
        return False
    try:
        conn = get_conn(db_path)
    except Exception:
        return False
    try:
        row = _latest(conn, restaurant_id, key)
        if row is None:
            return False
        n = conn.execute("UPDATE rec_instances SET tracker_id=? WHERE rec_id=? AND tracker_id IS NULL",
                         (tracker_id, row["rec_id"])).rowcount
        conn.commit()
        return bool(n)
    except Exception as e:
        print(f"[rec_ledger] link_tracker failed: {e}")
        return False
    finally:
        conn.close()


CHECKIN_ANSWERS = ("yes", "no", "partly")


def checkin_episode(restaurant_id, key=None, tracker_id=None, db_path=DB_PATH):
    """The episode a check-in answers, as a dict, or None (K1):

      tracker_id given  the episode that tracker measures
                        (rec_instances.tracker_id) for THIS restaurant — the
                        result the owner was asked about, however many times
                        the key was shown since. With `key` too, a key that
                        is not that episode's is None.
      key only          the key's latest episode that has a tracker, else
                        its latest.

    Resolving by key alone answered the key's LATEST episode: a card shown
    again after its tracker started took the "No, I didn't do it" meant for
    the measured one, and the result it was about kept counting (re-audit
    B1). Never raises."""
    if not restaurant_id or (tracker_id is None and not key):
        return None
    try:
        conn = get_conn(db_path)
    except Exception:
        return None
    try:
        if tracker_id is not None:
            row = conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND tracker_id=? "
                               "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                               (restaurant_id, int(tracker_id))).fetchone()
            if row is not None and key and row["key"] != str(key).strip()[:160]:
                row = None
        else:
            k = str(key).strip()[:160]
            row = (conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND key=? AND tracker_id IS NOT NULL "
                                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (restaurant_id, k)).fetchone()
                   or _latest(conn, restaurant_id, k))
        return dict(row) if row else None
    except Exception as e:
        print(f"[rec_ledger] checkin episode lookup failed: {e}")
        return None
    finally:
        conn.close()


def checkin(restaurant_id, key, did_it, conditions_changed=False, note=None, user_id=None, role=None,
            surface=None, db_path=DB_PATH, tracker_id=None, rec_id=None):
    """The owner's check-in on a recommendation: did they make the change,
    and did anything else change in those weeks (ROI audit #21's question,
    asked when a result lands). Recorded as a `checkin` event on the episode
    it answers — `rec_id` when the caller has resolved it, else
    checkin_episode(key, tracker_id); None when there is none (nothing to
    check in on).

    The event's meta — the field the outcome evaluation may read:
      did_it              "yes" | "no" | "partly"
      conditions_changed  bool — something else changed over the window
      note                the owner's words, ≤ 300 chars, or None
      tracker_id          the tracker measuring the episode, or None
      attribution         {"implemented": did_it, "confounded":
                           conditions_changed, "discount": bool} — discount is
                           true when did_it is "no" or conditions_changed:
                           a measured move on that tracker should not be read
                           as this recommendation working
    "yes" also records THAT episode as implemented (the owner says the
    change was made), once."""
    if did_it not in CHECKIN_ANSWERS:
        raise ValueError("did_it must be yes, no or partly")
    key = str(key or "").strip()[:160]
    if not restaurant_id or (not key and tracker_id is None and not rec_id):
        return None
    if rec_id:
        try:
            conn = get_conn(db_path)
        except Exception:
            return None
        try:
            got = conn.execute("SELECT * FROM rec_instances WHERE rec_id=? AND restaurant_id=?",
                               (rec_id, restaurant_id)).fetchone()
        finally:
            conn.close()
        row = dict(got) if got else None
    else:
        row = checkin_episode(restaurant_id, key=key or None, tracker_id=tracker_id, db_path=db_path)
    if row is None or (key and row["key"] != key):
        return None
    key = row["key"]
    changed = bool(conditions_changed)
    meta = {"did_it": did_it, "conditions_changed": changed,
            "note": (str(note).strip()[:300] or None) if note else None,
            "tracker_id": row["tracker_id"],
            "attribution": {"implemented": did_it, "confounded": changed,
                            "discount": did_it == "no" or changed}}
    ok = record(restaurant_id, key, "checkin", surface=surface, user_id=user_id, role=role, meta=meta,
                db_path=db_path, rec_id=row["rec_id"])
    if ok and did_it == "yes":
        record(restaurant_id, key, "implemented", surface=surface, user_id=user_id, role=role,
               meta={"via": "checkin"}, source_ref=f"checkin:{row['rec_id']}", db_path=db_path,
               rec_id=row["rec_id"])
    return dict(meta, rec_id=row["rec_id"], key=key, recorded=bool(ok))


def latest_checkin(restaurant_id, tracker_id=None, key=None, db_path=DB_PATH):
    """The newest check-in meta for a tracker (or a key), or None — what an
    outcome evaluation reads to discount a result the owner said they did
    not act on, or that something else moved. Never raises."""
    if not restaurant_id or (tracker_id is None and not key):
        return None
    try:
        conn = get_conn(db_path)
    except Exception:
        return None
    try:
        if tracker_id is not None:
            row = conn.execute(
                "SELECT e.meta, e.at FROM rec_events e JOIN rec_instances i ON i.rec_id=e.rec_id "
                "WHERE i.restaurant_id=? AND i.tracker_id=? AND e.event='checkin' ORDER BY e.at DESC, e.id DESC LIMIT 1",
                (restaurant_id, int(tracker_id))).fetchone()
        else:
            row = conn.execute("SELECT meta, at FROM rec_events WHERE restaurant_id=? AND key=? AND event='checkin' "
                               "ORDER BY at DESC, id DESC LIMIT 1", (restaurant_id, str(key)[:160])).fetchone()
        if not row:
            return None
        return dict(json.loads(row["meta"] or "{}") or {}, at=row["at"])
    except Exception as e:
        print(f"[rec_ledger] latest_checkin failed: {e}")
        return None
    finally:
        conn.close()


# ── missed detections (ROI audit #44) ────────────────────────────────────────
#
# A problem that reaches the owner as an alert, an issue or a close-out line
# is something Cavnar could have recommended about first. When one surfaces
# with no recommendation covering its subject in the MISSED_LOOKBACK_DAYS
# before, it is logged — the false negatives the acceptance figures cannot
# see. Admin-only (admin_ops.missed_detections).

MISSED_LOOKBACK_DAYS = 14
# A problem's own kind -> the recommendation kinds that would have covered
# it. A kind not listed here is covered by any recommendation of the same
# module (coarser, and said so on the admin page).
_LABOR_RECS = ("trim_day", "schedule_to_target", "labor_over", "overtime", "overtime_move", "optimizer",
               "intraday_pulse", "pulse_cut", "dsr_action", "schedule_")
_STOCK_RECS = ("stock_low", "critical_low", "dsr_action")
_REVIEW_RECS = ("top_issue", "diag_review", "insight_review", "link")
_STAFF_RECS = ("coverage", "cover", "standby", "schedule_", "optimizer", "dsr_action")
PROBLEM_COVERAGE = {
    "labor_over": _LABOR_RECS, "labor": _LABOR_RECS,              # the alert / the weekly labor issue
    "overtime": ("overtime", "overtime_move", "trim_day", "schedule_to_target", "dsr_action"),
    "food_waste": ("cut_waste", "diag_food", "food_cost_driver", "insight_food", "dsr_action"),
    "price_spike": ("price_spike", "food_cost_driver", "diag_food", "insight_food"),
    "critical_low": _STOCK_RECS, "stock_low": _STOCK_RECS, "stock": _STOCK_RECS,
    "neg_spike": _REVIEW_RECS, "negative_trend": _REVIEW_RECS, "rating_threshold": _REVIEW_RECS,
    "rating_drop": _REVIEW_RECS,
    "coverage": _STAFF_RECS, "callout": _STAFF_RECS, "no_show": _STAFF_RECS,
    "loss": ("loss",),
}
# Alert types that are a problem arriving (the rest are news, reminders or a
# single guest's review — no recommendation could have come first).
PROBLEM_ALERTS = ("labor_over", "food_waste", "critical_low", "price_spike", "neg_spike", "negative_trend",
                  "rating_threshold", "ai_visibility_drop", "coverage")


def _covering(problem_kind, module):
    kinds = PROBLEM_COVERAGE.get(problem_kind)
    return kinds, (module if not kinds else None)


def note_problem(restaurant_id, source, subject_key, module=None, detail=None, lookback_days=MISSED_LOOKBACK_DAYS,
                 at=None, db_path=DB_PATH) -> bool:
    """A problem surfaced (`source`: alert | issue | closeout | dsr) about
    `subject_key` ("labor_over:week", "negative_review:123", an alert type).
    Logs it when no recommendation covering it was shown to this restaurant
    in the `lookback_days` before: the same key, a kind in PROBLEM_COVERAGE,
    or — for a kind not mapped — any recommendation of the same module.
    Surfaces that are themselves the alert (alert_*, issue_sms) do not count
    as a recommendation made beforehand. Returns whether it was logged; one
    row per restaurant, source, subject and day. Never raises."""
    subject_key = str(subject_key or "").strip()[:160]
    if not restaurant_id or not subject_key:
        return False
    when = _stamp(at) or _now()
    since = (datetime.strptime(when, "%Y-%m-%d %H:%M:%S") - timedelta(days=int(lookback_days))
             ).strftime("%Y-%m-%d %H:%M:%S")
    pkind = kind_of(subject_key)
    kinds, same_module = _covering(pkind, module)
    try:
        conn = get_conn(db_path)
    except Exception:
        return False
    try:
        where = ["e.restaurant_id=?", "e.event='shown'", "e.at >= ?", "e.at <= ?",
                 "COALESCE(e.surface,'') NOT IN ('alert_sms','alert_email','alert_push','issue_sms')"]
        args = [restaurant_id, since, when]
        ors = ["i.key=?"]
        oargs = [subject_key]
        for k in kinds or ():
            if k.endswith("_"):
                ors.append("substr(i.kind, 1, ?)=?")
                oargs += [len(k), k]
            else:
                ors.append("i.kind=?")
                oargs.append(k)
        if same_module:
            ors.append("i.module=?")
            oargs.append(same_module)
        sql = (f"SELECT 1 FROM rec_events e JOIN rec_instances i ON i.rec_id=e.rec_id WHERE {' AND '.join(where)} "
               f"AND ({' OR '.join(ors)}) AND i.key NOT LIKE 'restore_kind:%' LIMIT 1")
        if conn.execute(sql, (*args, *oargs)).fetchone():
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO rec_missed_detections (restaurant_id, source, subject_key, module, detail, "
            "lookback_days, day, detected_at) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, str(source or "unknown")[:20], subject_key, (module or None),
             (str(detail)[:300] if detail else None), int(lookback_days), when[:10], when))
        conn.commit()
        return bool(cur.rowcount)
    except Exception as e:
        print(f"[rec_ledger] note_problem failed: {e}")
        return False
    finally:
        conn.close()


def backfill_tags(db_path=DB_PATH, limit=5000) -> int:
    """Tag episodes written before tags were stored (#17), and re-tag a dish
    or item episode stored with a day, day type or daypart read out of its
    NAME ("reprice:Friday Fish Fry" as a weekend recommendation — re-audit
    B14). Bounded; the filter is its own cursor (a re-tagged row no longer
    matches it), so the nightly job finishes the tail on later nights.
    Never raises."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return 0
    n = 0
    named = tuple(DISH_KINDS) + tuple(ITEM_KINDS)
    try:
        rows = conn.execute(
            "SELECT rec_id, restaurant_id, key, module, kind FROM rec_instances WHERE tags IS NULL "
            f"OR ((kind IN ({','.join('?' for _ in named)}) OR (kind='dsr_action' AND key LIKE 'dsr_action:%:food/%')) "
            "    AND (tags LIKE '%\"day:%' OR tags LIKE '%\"daytype:%' OR tags LIKE '%\"daypart:%')) "
            "LIMIT ?", (*named, int(limit))).fetchall()
        for r in rows:
            conn.execute("UPDATE rec_instances SET tags=? WHERE rec_id=?",
                         (_stored_tags(conn, r["restaurant_id"], r["key"], r["module"], r["kind"]), r["rec_id"]))
            n += 1
        conn.commit()
    except Exception as e:
        print(f"[rec_ledger] backfill_tags failed: {e}")
    finally:
        conn.close()
    return n


def episode_tags(row) -> list:
    """An episode's stored tags, or computed from its key when it predates
    them (no ingredient lookup — backfill_tags adds that)."""
    try:
        raw = row["tags"]
    except (IndexError, KeyError):
        raw = None
    if raw:
        try:
            return list(json.loads(raw) or [])
        except (TypeError, ValueError):
            pass
    return tags_for(row["key"], row["module"], row["kind"])


def unsilence(restaurant_id, key, db_path=DB_PATH) -> bool:
    """The owner took an answer back ("Use again"): the key can be shown."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return False
    try:
        n = conn.execute("UPDATE rec_instances SET silenced_until=NULL, snoozed_until=NULL WHERE restaurant_id=? AND key=?",
                         (restaurant_id, str(key or "")[:160])).rowcount
        conn.commit()
        return bool(n)
    finally:
        conn.close()


def silenced(restaurant_id, key, db_path=DB_PATH) -> bool:
    return str(key or "")[:160] in silenced_keys(restaurant_id, db_path=db_path)


def silenced_keys(restaurant_id, db_path=DB_PATH) -> set:
    """Every key an answer is currently silencing for this restaurant — on
    any surface. Includes Home's own dismissals, so an answer given before
    the ledger existed still holds."""
    now = _now()
    out = set()
    try:
        conn = get_conn(db_path)
    except Exception:
        return out
    try:
        for r in conn.execute(
                "SELECT key FROM rec_instances WHERE restaurant_id=? AND ((silenced_until IS NOT NULL AND silenced_until > ?) "
                "OR (status='open' AND snoozed_until IS NOT NULL AND snoozed_until > ?))", (restaurant_id, now, now)):
            out.add(r["key"])
        try:
            for r in conn.execute("SELECT key FROM home_dismissals WHERE restaurant_id=? AND expires_at > datetime('now')",
                                  (restaurant_id,)):
                out.add(r["key"])
        except Exception:
            pass
    except Exception as e:
        print(f"[rec_ledger] silenced_keys failed: {e}")
    finally:
        conn.close()
    return out


def expire_stale(db_path=DB_PATH, days=EXPIRE_AFTER_DAYS) -> int:
    """Close open episodes nobody answered within `days` of being created
    (is_stale) as expired — the 'shown and ignored' outcome the acceptance
    rate needs in its denominator. Bounded: at most 5000 per call."""
    now = datetime.utcnow()
    cutoff, now_s = stale_cutoff(days, now), now.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    n = 0
    try:
        rows = conn.execute("SELECT rec_id, restaurant_id, key FROM rec_instances WHERE status='open' AND created_at < ? "
                            "AND (snoozed_until IS NULL OR snoozed_until <= ?) LIMIT 5000", (cutoff, now_s)).fetchall()
        for r in rows:
            _close(conn, r["rec_id"], r["restaurant_id"], r["key"], "expired", meta={"reason": "no answer"})
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def _answered_near(conn, rid, key, events, at, not_dedupe=None) -> bool:
    """Whether the trail already holds one of `events` for this key within
    SAME_ANSWER_SECONDS of `at` — the answer a surface wrote itself when it
    was given, which an older ledger's row for it must not repeat."""
    t = _stamp(at)
    key = str(key or "").strip()[:160]
    if not t or not key or not events:
        return False
    marks = ",".join("?" for _ in events)
    span = f"{int(SAME_ANSWER_SECONDS)} seconds"
    return bool(conn.execute(
        f"SELECT 1 FROM rec_events WHERE restaurant_id=? AND key=? AND event IN ({marks}) "
        "AND at BETWEEN datetime(?, ?) AND datetime(?, ?) AND dedupe != ? LIMIT 1",
        (rid, key, *events, t, "-" + span, t, "+" + span, not_dedupe or "")).fetchall())


SYNC_TRACKERS_PER_PASS = 5000


def _sync_trackers(conn, limit=SYNC_TRACKERS_PER_PASS) -> dict:
    """Link every tracker to the episode it measures, for good, and carry
    its verdict (improved / worsened / no clear change / unknown) and its
    abandonment into that episode's trail (ROI audit #36).

    Not windowed: a tracker is carried whenever it still lacks its link, its
    verdict or its abandonment, however old — a 180-day tracker used to land
    after the 120-day sync window had passed it by, and an abandoned one was
    never carried at all. Bounded per pass; what is left is picked up next
    night (the NOT EXISTS makes the query its own cursor).

    The episode is the one the tracker is already linked to, else the one
    current when the tracker started (a minute's grace: Track writes the
    tracker a moment before the answer that opens a never-shown episode).
    An observed tracker ("observed:schedule_published:2026-09") measures
    something the owner did unprompted — no recommendation was shown, so
    there is no episode to measure."""
    out = {}
    try:
        rows = conn.execute(
            "SELECT o.id, o.restaurant_id, o.source_key, o.verdict, o.status, o.created_at "
            "FROM recommendation_outcomes o WHERE o.source_key NOT LIKE 'observed:%' "
            "AND EXISTS (SELECT 1 FROM rec_instances i WHERE i.restaurant_id=o.restaurant_id AND i.key=o.source_key "
            "            AND i.created_at <= datetime(o.created_at, '+60 seconds')) "
            "AND (NOT EXISTS (SELECT 1 FROM rec_instances i2 WHERE i2.tracker_id=o.id) "
            "  OR (o.status='evaluated' AND NOT EXISTS (SELECT 1 FROM rec_events e WHERE e.restaurant_id=o.restaurant_id "
            "      AND e.key=o.source_key AND e.dedupe='outcome:outcome:' || o.id)) "
            "  OR (o.status='abandoned' AND NOT EXISTS (SELECT 1 FROM rec_events e WHERE e.restaurant_id=o.restaurant_id "
            "      AND e.key=o.source_key AND e.dedupe='abandoned:outcome:' || o.id))) "
            "ORDER BY o.id LIMIT ?", (int(limit),)).fetchall()
    except Exception as e:           # no outcomes table on this database
        print(f"[rec_ledger] tracker sync skipped: {e}")
        return out
    for o in rows:
        rid, key = o["restaurant_id"], (o["source_key"] or "")[:160]
        ep = conn.execute("SELECT * FROM rec_instances WHERE tracker_id=? AND restaurant_id=? LIMIT 1",
                          (o["id"], rid)).fetchone()
        if ep is None:
            at = conn.execute("SELECT datetime(?, '+60 seconds')", (o["created_at"],)).fetchone()[0]
            ep = _episode_at(conn, rid, key, at)
            if ep is None:
                continue
            if conn.execute("UPDATE rec_instances SET tracker_id=? WHERE rec_id=? AND tracker_id IS NULL",
                            (o["id"], ep["rec_id"])).rowcount:
                out["linked"] = out.get("linked", 0) + 1
        at = _stamp(o["created_at"])
        if o["status"] == "evaluated":
            ref = f"outcome:outcome:{o['id']}"
            if not conn.execute("SELECT 1 FROM rec_events WHERE restaurant_id=? AND key=? AND dedupe=? LIMIT 1",
                                (rid, key, ref)).fetchone():
                verdict = o["verdict"] if o["verdict"] in ("improved", "worsened", "no_clear_change") else "unknown"
                if _add_event(conn, ep["rec_id"], rid, key, "outcome", dedupe=ref, at=at,
                              meta={"verdict": verdict, "tracker_id": o["id"]}):
                    out["outcomes"] = out.get("outcomes", 0) + 1
        elif o["status"] == "abandoned":
            ref = f"abandoned:outcome:{o['id']}"
            if not conn.execute("SELECT 1 FROM rec_events WHERE restaurant_id=? AND key=? AND dedupe=? LIMIT 1",
                                (rid, key, ref)).fetchone():
                if _add_event(conn, ep["rec_id"], rid, key, "abandoned", dedupe=ref, at=_now(),
                              meta={"tracker_id": o["id"]}):
                    out["abandoned"] = out.get("abandoned", 0) + 1
    conn.commit()
    return out


def sync_existing(db_path=DB_PATH, days=120) -> dict:
    """Carry answers the older ledgers hold into the trail: Home dismissals
    (and snoozes), outcome verdicts, issue resolutions, Ask confirms and
    dismissals, schedule recommendation answers. Scheduled nightly.

    Idempotent per (restaurant, key, answer) across every episode — the
    same row re-read each night is one answer, not one per new episode.
    Each answer attaches to the episode current when it was given (`at`)
    and silences from then, so it never closes a recommendation shown after
    it. A Home snooze stays a snooze (to its own expiry). Answers a surface
    already wrote to the trail itself are not written twice."""
    counts = {}

    def bump(k):
        counts[k] = counts.get(k, 0) + 1
    conn = get_conn(db_path)
    since = f"-{int(days)} days"
    try:
        try:
            home = conn.execute("SELECT restaurant_id, key, kind, dismissed_by, dismissed_at, expires_at FROM home_dismissals "
                                "WHERE dismissed_at >= datetime('now', ?)", (since,)).fetchall()
        except Exception:
            home = []
        try:
            iss = conn.execute("SELECT id, restaurant_id, source_key, status, resolved_at, acknowledged_at FROM ops_issues "
                               "WHERE source_key IS NOT NULL AND created_at >= datetime('now', ?)", (since,)).fetchall()
        except Exception:
            iss = []
        try:
            asks = conn.execute("SELECT id, restaurant_id, action, outcome, created_at, proposal_id FROM ask_cavnar_actions "
                                "WHERE created_at >= datetime('now', ?)", (since,)).fetchall()
        except Exception:
            asks = []
        try:
            sched = conn.execute("SELECT id, restaurant_id, kind, key, action, created_at FROM schedule_recommendation_events "
                                 "WHERE action IN ('accepted','dismissed') AND created_at >= datetime('now', ?)",
                                 (since,)).fetchall()
        except Exception:
            sched = []

        for r in home:
            rid, key, kind = r["restaurant_id"], r["key"], r["kind"]
            extra = {}
            if kind == "snooze":
                # "Not today" is a snooze to the row's own expiry — never a
                # fortnight's dismissal.
                ev, meta = "snoozed", {"until": r["expires_at"]}
                extra["snooze_until"] = _stamp(r["expires_at"])
            elif kind == "done":
                ev, meta = "completed", {"kind": "done"}
                extra["silence_until"] = r["expires_at"]
            else:
                ev, meta = "dismissed", {"kind": "not_for_us" if kind == "not_for_us" else "hide"}
                extra["silence_until"] = r["expires_at"]
            if _answered_near(conn, rid, key, (ev,), r["dismissed_at"]):
                continue            # home_brief.dismiss wrote this answer itself
            if record(rid, key, ev, surface="home", user_id=r["dismissed_by"], meta=meta,
                      source_ref=f"home:{key}:{r['dismissed_at']}", at=r["dismissed_at"], db_path=db_path, **extra):
                bump("home")
        for k, n in _sync_trackers(conn).items():
            counts[k] = counts.get(k, 0) + n
        for r in iss:
            # issues._resolve records the resolution under this same
            # source_ref; the acknowledgement arrives only through here.
            if r["resolved_at"]:
                if record(r["restaurant_id"], r["source_key"], "completed", surface="issue_sms",
                          source_ref=f"issue:{r['id']}:resolved", at=r["resolved_at"], db_path=db_path):
                    bump("issues")
            elif r["acknowledged_at"]:
                if record(r["restaurant_id"], r["source_key"], "accepted", surface="issue_sms",
                          source_ref=f"issue:{r['id']}:ack", at=r["acknowledged_at"], db_path=db_path):
                    bump("issues")
        for r in asks:
            ev = {"confirmed": "accepted", "dismissed": "dismissed"}.get(r["outcome"])
            if not ev:
                continue
            # Answers name the proposal they settle (ask:<proposal id>, the key
            # the card was shown under), and client_api records them under
            # "ask:<proposal id>:<outcome>" itself; an older client's answer
            # names no proposal.
            if r["proposal_id"]:
                akey, ref = rec_key("ask", r["proposal_id"]), f"ask:{r['proposal_id']}:{r['outcome']}"
            else:
                akey, ref = rec_key("ask", r["action"]), f"ask:{r['id']}"
            if _answered_near(conn, r["restaurant_id"], akey, (ev,), r["created_at"]):
                continue
            if record(r["restaurant_id"], akey, ev, surface="ask",
                      meta={"kind": "hide"} if ev == "dismissed" else None,
                      source_ref=ref, at=r["created_at"], db_path=db_path):
                bump("ask")
        from schedule_intel import schedule_rec_key
        for r in sched:
            # The key the recommendation was shown under (schedule_engine
            # presents schedule_rec_key) — a private truncation here filed
            # long sentences under a key nobody was shown.
            key = schedule_rec_key(r["kind"], r["key"])
            ev = "accepted" if r["action"] == "accepted" else "dismissed"
            # The button (strategy_routes._do_recommendation_event) writes
            # the trail itself; an edit that carried a recommendation out
            # (schedule_versions) reaches it only through here.
            if _answered_near(conn, r["restaurant_id"], key, (ev,), r["created_at"]):
                continue
            if record(r["restaurant_id"], key, ev, surface="schedule_review",
                      meta={"kind": "not_for_us"} if ev == "dismissed" else None,
                      source_ref=f"sched:{r['id']}", at=r["created_at"], db_path=db_path):
                bump("schedule")
    finally:
        conn.close()
    return counts


# ── one-off repair of what the replaying sync wrote (H-1) ────────────────────

_SYNC_DEDUPE_PATTERNS = ("dismissed:home:%", "completed:home:%", "completed:issue:%", "accepted:issue:%",
                         "accepted:ask:%", "dismissed:ask:%", "accepted:sched:%", "dismissed:sched:%",
                         "outcome:outcome:%")


def _sync_source_time(conn, ev):
    """(when the synced answer was really given, the direct events that
    would be the same answer) for one event the old sync wrote — or
    (None, ()) when its source row is gone and nothing can be said with
    certainty."""
    import re
    dd, key, event, rid = ev["dedupe"], ev["key"], ev["event"], ev["restaurant_id"]

    def one(sql, args):
        try:
            row = conn.execute(sql, args).fetchall()
        except Exception:
            return None
        return _stamp(row[0][0]) if row and row[0][0] else None
    home = f"{event}:home:{key}:"
    if dd.startswith(home):
        same = ("dismissed", "snoozed") if event == "dismissed" else (event,)
        return _stamp(dd[len(home):]), same
    m = re.fullmatch(r"(?:completed|accepted):issue:(\d+):(resolved|ack)", dd)
    if m:
        col = "resolved_at" if m.group(2) == "resolved" else "acknowledged_at"
        return one(f"SELECT {col} FROM ops_issues WHERE id=? AND restaurant_id=?", (int(m.group(1)), rid)), ()
    m = re.fullmatch(r"(?:accepted|dismissed):ask:(\d+)", dd)
    if m:
        return one("SELECT created_at FROM ask_cavnar_actions WHERE id=? AND restaurant_id=?",
                   (int(m.group(1)), rid)), (event,)
    m = re.fullmatch(r"(?:accepted|dismissed):sched:(\d+)", dd)
    if m:
        return one("SELECT created_at FROM schedule_recommendation_events WHERE id=? AND restaurant_id=?",
                   (int(m.group(1)), rid)), (event,)
    m = re.fullmatch(r"outcome:outcome:(\d+)", dd)
    if m:
        return one("SELECT created_at FROM recommendation_outcomes WHERE id=? AND restaurant_id=?",
                   (int(m.group(1)), rid)), ()
    return None, ()


_REPAIR_MARK = "rec_ledger:repair_sync_replays:v1"


def repair_sync_replays(db_path=DB_PATH, force=False) -> dict:
    """Undo what sync_existing wrote while it replayed old answers onto new
    episodes. A one-off: the nightly rec_ledger job calls it just before the
    sync (which then re-carries any removed answer onto the episode it
    really belongs to), and once it has completed it leaves a mark in
    job_cursors and does nothing on later nights. Idempotent regardless —
    the fixed sync writes nothing this matches — so `force` re-runs it
    safely.

    Removed, because each is certain from the rows themselves:
      * an answer written onto an episode that was created (and shown)
        after the answer was given — the source row still says when;
      * a Home "Not today" replayed as a 14-day dismissal, or any synced
        answer duplicating one the surface wrote itself — the direct event
        sits within SAME_ANSWER_SECONDS of the source row's own time.
    An episode left with no accept, complete or dismiss is reopened and its
    silence lifted (a lapsed one then expires on the normal rule).

    Left alone: a synced event whose source row has since gone (a replaced
    home_dismissals row, a pruned table) — without it the time of the
    answer is unknown, so a wrong attachment cannot be told from a right
    one."""
    out = {"removed": 0, "reopened": 0}
    conn = get_conn(db_path)
    try:
        if not force:
            try:
                if conn.execute("SELECT 1 FROM job_cursors WHERE key=?", (_REPAIR_MARK,)).fetchall():
                    return out
            except Exception:
                pass
        where = " OR ".join("e.dedupe LIKE ?" for _ in _SYNC_DEDUPE_PATTERNS)
        rows = conn.execute(
            "SELECT e.id, e.rec_id, e.restaurant_id, e.key, e.event, e.dedupe, e.at, i.created_at AS ep_created "
            f"FROM rec_events e JOIN rec_instances i ON i.rec_id=e.rec_id WHERE ({where})",
            _SYNC_DEDUPE_PATTERNS).fetchall()
        touched = set()
        for ev in rows:
            src, same = _sync_source_time(conn, ev)
            if not src:
                continue
            later = conn.execute("SELECT datetime(?, '+60 seconds') > ?", (src, ev["ep_created"])).fetchone()[0] == 0
            wrong_episode = later and bool(conn.execute(
                "SELECT 1 FROM rec_events WHERE rec_id=? AND event='shown' AND at <= ? LIMIT 1",
                (ev["rec_id"], ev["at"])).fetchall())
            duplicate = bool(same) and _answered_near(conn, ev["restaurant_id"], ev["key"], same, src,
                                                      not_dedupe=ev["dedupe"])
            if not (wrong_episode or duplicate):
                continue
            conn.execute("DELETE FROM rec_events WHERE id=?", (ev["id"],))
            out["removed"] += 1
            touched.add(ev["rec_id"])
        for rec_id in touched:
            still = conn.execute("SELECT 1 FROM rec_events WHERE rec_id=? AND event IN ('accepted','completed','dismissed') "
                                 "LIMIT 1", (rec_id,)).fetchall()
            if still:
                continue
            n = conn.execute("UPDATE rec_instances SET status='open', closed_at=NULL, silenced_until=NULL "
                             "WHERE rec_id=? AND status IN ('accepted','completed','dismissed')", (rec_id,)).rowcount
            out["reopened"] += n
        try:
            conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?, ?, datetime('now')) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (_REPAIR_MARK, json.dumps(out)))
        except Exception as e:           # no job_cursors table: it simply runs again next night
            print(f"[rec_ledger] repair mark not written: {e}")
        conn.commit()
    except Exception as e:
        print(f"[rec_ledger] repair_sync_replays failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    return out
