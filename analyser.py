import json
from models import update_analysis, get_pending_analysis
from ai_utils import (create_with_retry, extract_text, get_client, is_platform_stop,
                      is_refusal, model_for, parse_json_reply)
from ai_guard import UNTRUSTED_NOTE, wrap_untrusted


CATEGORIES = [
    "food_quality", "service", "wait_time", "value",
    "ambiance", "cleanliness", "reservation", "takeout_delivery"
]

# How a category is written when a person reads it. The stored values are
# snake_case identifiers — good for grouping, wrong for a sentence — and
# "takeout_delivery" reached the What Connects card on the dashboard looking
# like a variable name. Anything rendering a category goes through
# category_label(); nothing interpolates the raw key into prose.
CATEGORY_LABELS = {
    "food_quality": "food quality",
    "service": "service",
    "wait_time": "wait time",
    "value": "value for money",
    "ambiance": "atmosphere",
    "cleanliness": "cleanliness",
    "reservation": "reservations",
    "takeout_delivery": "takeout and delivery",
}


def category_label(category: str) -> str:
    """A category as prose. Falls back to the key with underscores opened
    up, so a category added to CATEGORIES without a label here still reads
    as words rather than code."""
    key = (category or "").strip().lower()
    return CATEGORY_LABELS.get(key, key.replace("_", " "))

# The operational vocabulary. Every value the model may return for a
# structured entity field is enumerated here and validated against the list,
# for the same reason `categories` is: an un-enumerated string becomes a
# bucket of one that no trend query can ever group with anything else.
DAYPARTS = ("breakfast", "brunch", "lunch", "happy_hour", "dinner", "late_night")
SERVICE_MODES = ("dine_in", "takeout", "delivery", "bar", "patio", "private_event")
STAFF_ROLES = ("server", "host", "bartender", "kitchen", "manager", "busser",
               "runner", "delivery_driver")

# How serious this is, beyond the binary urgency flag that drives alerting.
# Ordered most to least severe — index in this tuple IS the priority, so a
# caller can sort on it without a second lookup table.
#
# urgency="high" answers "wake the owner up?". This answers "where does it sit
# when the owner is ranking twelve complaints on a Tuesday morning?", which is
# the question the module could not represent at all: a parking gripe and a
# legal threat were both simply "normal" or both simply "high".
SEVERITIES = ("safety", "legal", "operational", "service", "minor")
SEVERITY_LABELS = {
    "safety":      "Guest safety",
    "legal":       "Legal exposure",
    "operational": "Operational failure",
    "service":     "Service quality",
    "minor":       "Minor",
}

ANALYSE_PROMPT = """You are analysing a restaurant review. Return ONLY valid JSON — no markdown, no commentary.

{untrusted_note}

Review:
Rating: {rating}/5
Text:
{text}

Return this exact shape:
{{
  "sentiment": "positive" | "neutral" | "negative",
  "categories": [list of 1-3 from: {categories}],
  "summary": "one sentence, max 20 words, owner perspective",
  "urgency": "high" | "normal",
  "severity": one of: {severities},
  "specific_complaint": "the single concrete thing that went wrong, max 8 words, or null if nothing did",
  "entities": {{
    "dishes": [menu items or drinks named IN THE REVIEW, max 3, exactly as the guest wrote them],
    "staff_roles": [list from: {roles}],
    "daypart": one of {dayparts} or null,
    "service_mode": one of {modes} or null
  }}
}}

EXTRACTION RULES — these bound what you may put in `entities`:
- Only extract what the review ITSELF states or unambiguously implies. Never infer a dish from a category, a role from a complaint, or a daypart from a rating.
- `daypart`: only if the review names a meal, a time, or an unambiguous occasion ("we came for brunch", "at 10pm", "after work drinks"). A review that just says "we came in Saturday" has no daypart — return null.
- `staff_roles`: only a role the review actually points at. "The service was slow" is not a role; "our server disappeared" is `server`; "the bartender was great" is `bartender`.
- `dishes`: the guest's own words, not a normalised menu name. If no food or drink is named, return an empty list.
- When you are unsure, return null or an empty list. An empty field is correct; a guessed one is a fabricated fact about this restaurant's operation.

severity is:
- "safety" — illness, food poisoning, allergic reaction, foreign object, injury on premises, or an active hazard
- "legal" — lawsuit, attorney, health department, BBB, discrimination, harassment, or a staff misconduct allegation
- "operational" — something broke in how the restaurant runs: wrong or missing order, very long wait, cold food, a reservation not honoured, a closure or availability failure
- "service" — the experience was poor but nothing failed outright: unfriendly, inattentive, rushed, noisy
- "minor" — a preference, a small gripe, or a positive review with no complaint in it

urgency is "high" if ANY of these are present:
- Food safety, illness, food poisoning, allergic reaction, foreign object in food
- Physical injury on premises
- Legal threats, lawsuit, attorney
- Explicit threat to contact health department or BBB
- Threatening, abusive, or discriminatory language directed at staff
- Direct staff misconduct complaint (harassment, theft, dishonesty)"""


SENTIMENTS = ("positive", "neutral", "negative")
URGENCIES = ("high", "normal")


# A star rating is a fact the guest chose. Sentiment is the model's reading
# of the text. They can legitimately differ in the middle, but not at the
# ends: a 1-star is not a positive review and a 5-star is not a negative
# one, whatever the prose does. An LLM reading "the staff were lovely but I
# was ill afterwards" as positive dropped that review out of the negative
# spike count and drew it as a positive on the sentiment chart.
def _sentiment_floor(rating: int, sentiment: str) -> str:
    try:
        r = int(rating)
    except (TypeError, ValueError):
        return sentiment
    if r <= 2 and sentiment == "positive":
        return "negative"
    if r >= 5 and sentiment == "negative":
        return "neutral"
    return sentiment


def _clean_dish(raw: str) -> str:
    """A dish name the guest wrote, trimmed to something a trend query can
    group on. Lowercased and whitespace-collapsed so "Truffle Fries" and
    "truffle  fries" are one bucket, capped so a model that returns half the
    review as a "dish" cannot poison the cluster."""
    name = " ".join(str(raw or "").split()).strip(" .,!?\"'").lower()
    if len(name) < 2 or len(name) > 40:
        return ""
    # A "dish" that is really a sentence is the model paraphrasing, not
    # naming. Five words is generous for "the bone-in ribeye special".
    if len(name.split()) > 5:
        return ""
    return name


def _validate_entities(raw):
    """Coerce the model's `entities` object into the enumerated vocabulary.

    Same discipline as `categories`: anything outside the enum is dropped
    rather than stored, because an un-enumerated value is a cluster of one
    that no trend query will ever group with anything else — and it would be
    rendered to the owner as if it were one of ours. Returns None when nothing
    survived, so "the model gave us nothing" and "the review mentioned nothing"
    are both stored as absent rather than as an empty shape that reads like a
    measured result.
    """
    if not isinstance(raw, dict):
        return None
    dishes, seen = [], set()
    rd = raw.get("dishes")
    if isinstance(rd, str):
        rd = [rd]
    for d in (rd or [])[:5]:
        name = _clean_dish(d)
        if name and name not in seen:
            seen.add(name)
            dishes.append(name)
        if len(dishes) >= 3:
            break
    rr = raw.get("staff_roles")
    if isinstance(rr, str):
        rr = [rr]
    roles = []
    for r in (rr or []):
        v = str(r).strip().lower().replace(" ", "_")
        if v in STAFF_ROLES and v not in roles:
            roles.append(v)
    daypart = str(raw.get("daypart") or "").strip().lower().replace(" ", "_")
    daypart = daypart if daypart in DAYPARTS else None
    mode = str(raw.get("service_mode") or "").strip().lower().replace(" ", "_")
    mode = mode if mode in SERVICE_MODES else None
    out = {}
    if dishes:  out["dishes"] = dishes
    if roles:   out["staff_roles"] = roles
    if daypart: out["daypart"] = daypart
    if mode:    out["service_mode"] = mode
    return out or None


def _severity_floor(rating: int, urgency: str, severity: str) -> str:
    """Keep severity consistent with the two facts we already hold.

    A review the model itself flagged urgent cannot be "minor" or "service" —
    urgency="high" is defined by safety, injury, legal threat or misconduct,
    so the severity tier has to be one of the two that mean the same thing.
    And a 5-star review with no complaint is not an operational failure
    whatever prose the model produced. Without this the priority ordering
    could contradict the alerting that already fired on the same row.
    """
    sev = severity if severity in SEVERITIES else None
    if str(urgency or "").lower() == "high":
        return sev if sev in ("safety", "legal") else "safety"
    try:
        if int(rating) >= 5 and sev in ("safety", "legal", "operational"):
            return "minor"
    except (TypeError, ValueError):
        pass
    # No tier from the model is no tier — stored NULL and counted as
    # unclassified everywhere (H14). This used to default to "minor" here
    # while complaint_clusters defaulted the same NULL to "service", so one
    # review sat in two different tiers depending on the screen.
    return sev


# A review with no severity tier. Never folded into a tier: counted apart
# (severity_breakdown's and each cluster's `unclassified`), and ranked like
# UNCLASSIFIED_RANKS_AS only where a sort needs a position for it.
UNCLASSIFIED = "unclassified"
UNCLASSIFIED_RANKS_AS = "service"


def _validate_analysis(result, rating: int = None, text: str = None):
    """Coerce the model's JSON into what the schema and the UI can hold.

    The reviews table's own CHECK constraints were doing this job, which
    meant an out-of-enum value surfaced as a sqlite IntegrityError swallowed
    by the caller's `except Exception: print(...)` — and the review stayed
    unanalysed, so its urgency alert never fired. `categories` had no
    constraint at all, so an invented category was stored and shown in the
    topic heatmap as if it were one of ours.
    """
    if not isinstance(result, dict):
        raise ValueError("analysis was not a JSON object")
    sentiment = str(result.get("sentiment") or "").strip().lower()
    if sentiment not in SENTIMENTS:
        raise ValueError(f"sentiment {result.get('sentiment')!r} is not one of {SENTIMENTS}")
    urgency = str(result.get("urgency") or "normal").strip().lower()
    if urgency not in URGENCIES:
        urgency = "normal"
    raw_cats = result.get("categories")
    if isinstance(raw_cats, str):
        raw_cats = [raw_cats]
    cats = [c for c in (str(x).strip().lower() for x in (raw_cats or [])) if c in CATEGORIES][:3]
    summary = " ".join(str(result.get("summary") or "").split())[:300]
    if not summary:
        raise ValueError("analysis had no summary")
    if rating is not None:
        sentiment = _sentiment_floor(rating, sentiment)
    complaint = " ".join(str(result.get("specific_complaint") or "").split())[:120] or None
    if complaint and complaint.lower() in ("null", "none", "n/a"):
        complaint = None
    severity = _severity_floor(rating, urgency,
                               str(result.get("severity") or "").strip().lower())
    model_urgency = urgency
    urgency, escalated = _escalate_urgency(rating, urgency, severity, text)
    out = {"sentiment": sentiment, "categories": cats, "summary": summary,
           "urgency": urgency, "severity": severity,
           "specific_complaint": complaint,
           "entities": _validate_entities(result.get("entities")),
           "model_urgency": model_urgency}
    if escalated:
        out["urgency_escalated"] = escalated
    return out


# Keywords strong enough that, on a 1–2★ review, code raises the alert
# whatever the model said (R4). A subset of notify.HEALTH_KEYWORDS: the ones
# a complaint uses literally. "hospital" (a nurse from the hospital), "rat "
# (it is inside "great ") and "mold" (molded chocolate) are left to the
# model.
STRONG_HEALTH_KEYWORDS = (
    "food poison", "foodborne", "sick after", "got sick", "felt sick", "vomit", "threw up", "throw up",
    "diarrhea", "nausea after", "ill after", "health department", "health inspector", "cockroach", "roach",
    "rodent", "bug in ", "insect in", "foreign object", "glass in", "metal in", "hair in", "raw chicken",
    "undercooked chicken", "salmonella", "ecoli", "e. coli")
LOW_RATING_ESCALATES = 2


def _escalate_urgency(rating, urgency, severity, text=None):
    """(urgency, why) with the safety call made in code where the model's
    own fields contradict it (R4, B5 #4). `_severity_floor` raised severity
    from urgency, never urgency from severity, so a food-poisoning review
    the model scored urgency=normal but severity=safety fired no alert. Now
    a safety or legal severity, or a strong health keyword on a review of
    LOW_RATING_ESCALATES stars or fewer, makes it urgent; `why` says which
    (None when the model's urgency stands)."""
    if str(urgency or "").lower() == "high":
        return "high", None
    if severity in ("safety", "legal"):
        return "high", f"the analysis rated its severity {severity}"
    try:
        low = rating is not None and int(rating) <= LOW_RATING_ESCALATES
    except (TypeError, ValueError):
        low = False
    if low and text:
        import unicodedata
        t = unicodedata.normalize("NFKC", text).lower()
        hits = [k for k in STRONG_HEALTH_KEYWORDS if k in t]
        if hits:
            return "high", f"a {int(rating)}-star review mentions {hits[0].strip()!r}"
    return urgency, None


def analyse_review(review_id: int, rating: int, text: str, restaurant_id: int = None) -> dict:
    prompt = ANALYSE_PROMPT.format(
        rating=rating,
        # Fenced and labelled: this is text a stranger wrote, and it used to
        # be interpolated raw with nothing but a quote swap between it and
        # the instructions above it.
        text=wrap_untrusted(text),
        untrusted_note=UNTRUSTED_NOTE,
        categories=", ".join(CATEGORIES),
        severities=", ".join(SEVERITIES),
        roles=", ".join(STAFF_ROLES),
        dayparts=", ".join(DAYPARTS),
        modes=", ".join(SERVICE_MODES),
    )
    message = create_with_retry(
        get_client(),
        model=model_for("review_analysis"),
        # The schema grew an entities object and two more fields; 256 tokens
        # was already close enough to the old ceiling that a review naming
        # three dishes would have tripped the truncation guard below.
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant_id,
        action="review_analysis",
    )
    if getattr(message, "stop_reason", None) == "max_tokens":
        raise ValueError("analysis was truncated")
    if is_refusal(message):
        raise ValueError("the model declined to analyse this review")
    # A leading "Here is the JSON:" or a code fence used to fail json.loads
    # and cost the review one of its five attempts (AI-26).
    result = _validate_analysis(parse_json_reply(extract_text(message), expect=dict),
                                rating=rating, text=text)
    update_analysis(
        review_id,
        result["sentiment"],
        result["categories"],
        result["summary"],
        result["urgency"],
        entities=result.get("entities"),
        specific_complaint=result.get("specific_complaint"),
        severity=result.get("severity"),
    )
    # Safety rests on Haiku alone once a review is analysed (notify.
    # _is_health_alert). Its verdict stands — its negation reading is why
    # the keyword list is only a fallback — but a keyword hit it read as
    # "normal" is logged, once per analysis, so how often the two disagree
    # is a rate rather than a guess; auto-approve keeps those reviews out
    # (models.auto_approve_candidates) (H5).
    # The disagreement is the MODEL's (its own urgency), logged even when
    # code escalated the review (R4) — it is still the evidence of how often
    # Haiku misses one.
    if result.get("model_urgency", result.get("urgency")) != "high":
        try:
            import notify
            hits = notify.health_keyword_hits(text)
            if hits:
                notify.record_safety_disagreement(hits, restaurant_id=restaurant_id, review_id=review_id)
        except Exception as e:
            print(f"    [{review_id}] safety disagreement check failed: {e}")
    return result


def analyse_pending(restaurant_id: int, limit: int = 500):
    """Analyse every review still waiting.

    The default was 50. A review left unanalysed has no urgency, so its
    health/safety alert never fires, and it was invisible to the owner's
    totals entirely until get_review_stats stopped filtering on processed=1.
    A CSV import routinely exceeds 50, and nothing came back for the rest.
    """
    reviews = get_pending_analysis(restaurant_id, limit)
    print(f"  Analysing {len(reviews)} reviews...")
    results = []
    for r in reviews:
        try:
            # restaurant_id was omitted here, so every review analysis went
            # into ai_usage unattributed and the per-restaurant spend cap
            # evaluated with no restaurant to cap.
            res = analyse_review(r.id, r.rating, r.text, restaurant_id=restaurant_id)
            flag = " *** URGENT ***" if res.get("urgency") == "high" else ""
            print(f"    [{r.id}] {res['sentiment']:8s} | {', '.join(res['categories'])}{flag}")
            results.append({"id": r.id, **res})
        except Exception as e:
            if is_platform_stop(e):
                # The budget ceiling or the provider breaker, not this review:
                # every review after it would hit the same stop. Counting it
                # as an attempt spent all five on one bad afternoon and the
                # review was never analysed again (AI-4). Stop the pass and
                # leave the queue for the next one.
                print(f"    analysis paused: {e}")
                break
            # An unanalysed review has no urgency, so its health/safety alert
            # never fires. That is not something to print to stdout and move
            # on from — it reaches the daily failure digest.
            print(f"    [{r.id}] ERROR: {e}")
            try:
                from models import record_ai_attempt
                record_ai_attempt(r.id, "analysis")
            except Exception:
                pass
            try:
                import ops
                ops.capture(e, job="review_analysis",
                            context=f"restaurant_id={restaurant_id} review_id={r.id}")
            except Exception:
                pass
    return results
