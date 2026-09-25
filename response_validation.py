"""
response_validation.py — the Response Validation Layer: one deterministic
check between what a model writes and what an owner (or a guest) reads.

The audit that asked for it ("Never Say", 9/24/26, NS1–NS6) found that every
surface ran a different subset of the ai_guard checks, and that none of them
looked at the claim AROUND a figure: an opportunity called "saved", a
projection called "measured", "will" and "guaranteed" at 38% confidence, a
hedged diagnosis restated as a definite cause, an invented industry average,
another tenant's name, a missing-data caveat the prompt asked for and the
model left out. This module is that check, as rules, not as prompt text.

    from response_validation import validate, ValidationContext, Fact
    v = validate(text, ValidationContext(restaurant_id=rid, surface="labor_insight",
                                         facts=[Fact("labor.gap_monthly", 867, "$", "opportunity", "month")],
                                         cause_anchors=[{"text": diag.cause, "strength": "likely"}],
                                         confidence=k1))
    v.text        # the text after deterministic rewrites ("" when refused)
    v.verdict     # pass | caveat | withhold | refuse
    v.findings    # [{rule, severity, span, detail, action}]
    v.actions     # {controls, caveats, dropped, rewrites}

PURE (layer 0): no database, no clock, no model, no network. It imports only
ai_guard and confidence_engine's thresholds. The one exception is log(),
which lazily calls ai_utils.log_validation (layer 1) and never raises, and
mode_for(), which reads RESPONSE_VALIDATION_MODE from the environment.

THE RULES (codes are stable; PROMPT_LIBRARY.md → Response Validation):
  F1 a figure must be one of the facts (DSR written-precision tolerance;
     hedged "about $1,200" may round to its zeros)      withhold / drop
  F2 a figure in a sentence naming an entity belongs to it withhold / drop
  F3 a money period must match the fact's, both ways: "a month" on a
     period-less fact, "on pace" on a non-projection     withhold / drop
  F4 kind flip: saved / recovered / losing / will save only on a measured,
     DELIVERED fact; an opportunity is rewritten to "about $X a month is
     available (an opportunity, not money saved)"; an estimate gets "could"
     and "about"; an opportunity with no hedge is labelled   rewrite, else
                                                          withhold / drop
  F5 a figure that is a sum of facts of different kinds       drop
  F6 an estimate / projection quoted without saying so → "about" /
     "a projected" is inserted                                rewrite
  F7 an annual (or rate-scaled) figure: a projection word and ≥ 28 data
     days                                   rewrite, else withhold / drop
  F8 unit-typed: a % is never backed by a bare "30 days"   withhold / drop
  C1 certainty vs the K1 %: ≥75 "should", 50–74 "could", <50 or unmeasured
     "might"; guaranteed / definitely / certainly / without a doubt /
     proves / ensures / 100% sure always go; always / never about an
     outcome become often / rarely                            rewrite
  C2 the model's own confidence ("I'm 90% sure", "high confidence") goes
     (ai_guard.rewrite_confidence_claims)                      rewrite
  K1 a causal connective (the full list — since, when, following, is why,
     the reason, behind, responsible for, blame, triggered, explains, after,
     led to, drove, paid off, tied to, linked to, as a result …) may not
     state more than its anchor's strength: supported ≥ likely ≥
     association. Too strong → rewritten down ("caused" → "may have
     contributed to"); no anchor but a fact on both sides → "moved
     together" wording; no anchor at all                 caveat / drop
  X1 a stated direction that disagrees with the fact's     caveat / drop
  M1 a required disclosure (hours estimated, partial period, stale source,
     low coverage, few reviews, missing sales) that the text leaves out →
     its standard caveat; sample data → refuse           caveat / refuse
  M2 a topic whose input was missing (weather, demand, covers,
     reservations, guests/sales on marketing)             caveat / drop
  B1 a peer or industry claim — the whole grammar (_BENCH_RE): "industry
     average / band / median", "most [sports] bars", "similar / comparable
     / peer / typical <group>", "similar to yours", "like yours", "top /
     bottom quartile | quarter | half | decile | N%", "above the median
     for", "beats N% of", "best-in-class", "top performers", "area
     average" — binds to a benchmark fact (kind "benchmark") or is not
     said:
       * a figure no benchmark fact holds is dropped, interactive too (a
         guessed "most restaurants run 31%" never reaches an owner);
       * a claim with no figure binds to a benchmark fact for the SAME
         metric as its subject (labor, food cost, rating, replies …),
         else caveat / drop;
       * a Cavnar peer group (source_kind "cohort", or the all-types group)
         needs n ≥ the fact's min_n (else COHORT_MIN_N, 8); a published
         figure (source_kind published / rule_of_thumb / vendor) needs a
         source and a year, never an n;
       * the all-types group is recognised by source.restaurant_category
         == "platform" or engine_kind == "platform", never by its label;
         it is cited "N other restaurants on Cavnar, all types", and "like
         yours" / "similar to yours" on it is rewritten to that;
       * a bound claim names its group: "(N other <group>, as of M/D/YY)"
         for a peer group, "(source year)" for a published figure;
       * a ranking word ("top quartile", "well above", "percentile") needs
         the fact's strength_pct ≥ STRONG_CLAIM_PCT (75); below it the
         phrase becomes "about the middle" when the fact's standing says
         so, else caveat / drop; a ranking the fact's standing
         contradicts is caveat / drop         rewrite / caveat / drop
  P2 a prediction about similar restaurants ("restaurants like / similar
     to yours reduced / cut / improved X by N%") needs a fact of kind
     "prediction" whose value matches N at its written precision and whose
     source.n_restaurants ≥ PREDICTION_MIN_RESTAURANTS (5); with none the
     sentence is dropped. Likelihood words in it ("high likelihood", "very
     likely", "likely") are capped by the K1 target level of the
     recommendation's own confidence, never by the size of the effect.
     The prediction fact (the Restaurant DNA layer builds it):
       Fact(key="predict.<rec_kind>.<metric>.effect_pct", value=11.0,
            unit="%", kind="prediction", as_of="9/20/26",
            source={"source_kind": "prediction", "metric": <metric>,
                    "rec_kind": <kind>, "n_restaurants": 7, "n_orgs": 7,
                    "n_results": 14, "min_n": 5, "interval": [4.0, 17.0],
                    "similarity_pct": 82})
     value is the magnitude of the median measured effect (advice taken
     minus not taken) and interval its 80% range; a range spanning 0 is
     "mixed results" and is never emitted as a prediction fact
                                                      drop / rewrite
  N1 a name the input never held (ai_guard.unsupported_names)
                                   caveat / drop; a diagnosis refuses
  T1 another tenant's name, or "a restaurant like yours runs X"
                                                         drop / refuse
  A1 "I've sent / posted / ordered / texted …" → "queued … for your OK"
                                                               rewrite
  A2 unsafe actions: discipline of staff, food-safety shortcuts, a cut
     below a role floor (policy role_floors; a role with none is held to
     policy cut_floor_default, else CUT_FLOOR_DEFAULT = 2) or of a
     keyholder, a flat "that's legal / you're compliant"
                                             drop (legal: caveat / drop)
  P1 public text: allergen / free-from / "safe for", fault and
     responsibility, inspection and compliance claims, comps ("on me",
     "% off", "free"), "fixed / won't happen again / make it right /
     guarantee", awards ("voted", "#1", "best in", "award-winning",
     "famous") and offers the owner never wrote, the never-say list,
     ai_guard.check_public_reply / check_marketing_copy          refuse
  I1 injection residue, a model-written "UNVERIFIED:" marker, and (on
     unattended owner surfaces) a six-word echo of untrusted text
                                                 drop (public: refuse)

SEVERITY → ACTION: info (log) · rewrite (deterministic phrase swap) ·
caveat (a structured actions.caveats entry — never only the "UNVERIFIED:"
string) · withhold (actions.controls = False, plus its caveat) · drop (the
sentence, or the line under validate_lines) · refuse (the caller's
fallback: fixed copy or the previous stored read; Verdict.text is "").
Unattended delivery turns anything above caveat into drop/refuse; public
text (audience guest_public) turns anything above rewrite into refuse.

PROPERTIES (tests/test_response_validation.py): validate never adds a
figure, never raises confidence (it only ever lowers a modal, a band or a
causal level), and is idempotent (a second pass makes no new rewrites).
"""
import bisect
import functools
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from itertools import combinations

import ai_guard as _g
from confidence_engine import HIGH_AT, MEDIUM_AT

VERSION = "rv1"

# ── closed vocabularies ─────────────────────────────────────────────────────

SURFACES = (
    "ask",
    "review_insight", "food_insight", "labor_insight", "marketing_insight",
    "intel",
    "review_diagnosis", "food_diagnosis",
    "digest", "dsr", "weekly_plan", "schedule_note", "email_personalise",
    "reply_public", "guest_sms", "social_post", "calendar_idea",
)
AUDIENCES = ("owner", "manager", "guest_public", "internal")
DELIVERIES = ("interactive", "unattended")
FACT_KINDS = ("measured", "computed", "estimate", "opportunity", "projection", "plan", "forecast",
              "benchmark", "price", "prediction")
VERDICTS = ("pass", "caveat", "withhold", "refuse")
SEVERITIES = ("info", "rewrite", "caveat", "withhold", "drop", "refuse")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
_ACTION_OF = {"info": "log", "rewrite": "rewrite", "caveat": "caveat", "withhold": "withhold",
              "drop": "drop", "refuse": "refuse"}

# Surfaces nobody reads before the recipient does, and surfaces a guest reads.
UNATTENDED_SURFACES = frozenset({"digest", "dsr", "weekly_plan", "email_personalise"})
PUBLIC_SURFACES = frozenset({"reply_public", "guest_sms", "social_post", "calendar_idea"})
DIAGNOSIS_SURFACES = frozenset({"review_diagnosis", "food_diagnosis"})

RULES = {
    "F1": "figure not in the facts",
    "F2": "figure attached to the wrong entity",
    "F3": "money period differs from the fact's",
    "F4": "estimate, opportunity or plan worded as money saved, lost or promised",
    "F5": "sum across kinds",
    "F6": "estimate or projection quoted without saying so",
    "F7": "annual or rate-scaled figure without a projection label or enough data",
    "F8": "figure backed only by a different unit",
    "C1": "certainty stronger than the confidence",
    "C2": "the model's own confidence",
    "K1": "causal claim stronger than its anchor",
    "X1": "direction contradicts the data",
    "M1": "required disclosure missing",
    "M2": "claim about an input that was missing",
    "B1": "benchmark claim without a registered benchmark",
    "P2": "prediction about similar restaurants without a prediction fact",
    "N1": "name not in the input",
    "T1": "another tenant's name or a member's value",
    "A1": "claims an action was taken",
    "A2": "unsafe action or flat legal statement",
    "P1": "public text claim the restaurant never made",
    "I1": "injection residue or echo of untrusted text",
}

# The disclosure codes M1 knows: the words that show a disclosure is
# already there, and the standard caveat (NS6 §D) attached when it is not.
DISCLOSURES = {
    "hours_estimated": (r"\b(?:scheduled\s+hours?|estimated\s+hours?|hours?\s+(?:are|were)\s+(?:estimated|scheduled)|"
                        r"from\s+the\s+schedule|scheduled,?\s+not\s+clocked|not\s+clocked)\b",
                        "Every hour figure here is the scheduled hour, not a clocked one."),
    "partial_period": (r"\b(?:partial|so\s+far|to\s+date|only\s+\d+\s+(?:days?|nights?|weeks?)|incomplete|"
                       r"not\s+a\s+full)\b",
                       "This covers a partial period, not a full one."),
    "stale_source": (r"\b(?:as\s+of|data\s+through|out\s+of\s+date|stale|last\s+(?:synced|updated)|"
                     r"hasn['’]?t\s+synced|not\s+synced|older\s+data)\b",
                     "Some of this data is out of date."),
    "low_coverage": (r"\b(?:partial|coverage|only\s+covers|not\s+every\s+day|missing\s+days|incomplete|"
                     r"\d+\s+of\s+\d+\s+days)\b",
                     "Not every day in this window has data."),
    "low_review_count": (r"\b(?:only\s+\d+\s+reviews?|few\s+reviews|a\s+(?:small|handful)|rests\s+on\s+\d+|"
                         r"(?:one|two|three|four|five)\s+reviews?)\b",
                         "This rests on only a few reviews."),
    "missing_sales": (r"\b(?:no\s+sales|sales\s+(?:are|were|is)\s+missing|without\s+sales|sales\s+data\s+"
                      r"(?:is|was)\s+missing|no\s+sales\s+data)\b",
                      "Sales data is missing for part of this window."),
    "estimated_figures": (r"\bestimat", "Figures here are estimates, not measurements."),
}
_DISCLOSURE_RE = {k: re.compile(p, re.I) for k, (p, _c) in DISCLOSURES.items()}

# M2: what a sentence mentions when it talks about an input.
TOPICS = {
    "weather": r"\b(?:rain(?:y|ed|ing)?|snow(?:y|ed|ing)?|storms?|heat\s*wave|hot\s+weather|cold\s+snap|weather|"
               r"temperatures?|degrees|sunny|forecast\s+calls)\b",
    "demand": r"\b(?:busier|slower\s+than\s+usual|demand|foot\s+traffic|walk-?ins|expected\s+(?:rush|crowd))\b",
    "covers": r"\b(?:covers|guest\s+counts?|headcount|table\s+turns|foot\s+traffic)\b",
    "reservations": r"\b(?:reservations?|bookings?|booked|resy|opentable|tock)\b",
    "guests": r"\b(?:guests|covers|sales|revenue|bookings|traffic|diners|customers)\b",
    "sales": r"\b(?:sales|revenue)\b",
    "reviews": r"\b(?:reviews?|rating|stars?)\b",
    "labor": r"\b(?:labor|labour|payroll|staffing)\b",
}
_TOPIC_RE = {k: re.compile(v, re.I) for k, v in TOPICS.items()}
_OUTCOME_MOVE_RE = re.compile(
    r"\b(?:bring(?:ing)?\s+in|brought\s+in|more|fewer|up|down|rose|fell|lost|won|drew|draws?|driving|drove|"
    r"fill(?:s|ed|ing)?|grew|jumped|dropped|stronger|weaker|increase[ds]?|decrease[ds]?)\b", re.I)


# ── the inputs ──────────────────────────────────────────────────────────────

_UNIT_NORM = {"$": "$", "usd": "$", "money": "$", "dollars": "$", "dollar": "$",
              "%": "%", "pct": "%", "percent": "%", "rate": "%",
              "pts": "pts", "pt": "pts", "points": "pts", "pp": "pts",
              "★": "★", "star": "★", "stars": "★", "rating": "★",
              "count": "count", "n": "count", "#": "count",
              "h": "h", "hours": "h", "hrs": "h", "hour": "h",
              "x": "x", "ratio": "x", "times": "x", "": ""}
_PERIOD_NORM = {"wk": "week", "weekly": "week", "week": "week", "mo": "month", "monthly": "month",
                "month": "month", "yr": "year", "yearly": "year", "annually": "year", "annual": "year",
                "year": "year", "daily": "day", "day": "day", "nightly": "night", "night": "night",
                "tonight": "night", "today": "day", "shift": "night", "period": "period", "window": "period"}


def _norm_direction(d):
    if d in (None, "", 0):
        return None
    if isinstance(d, (int, float)) and not isinstance(d, bool):
        return 1 if d > 0 else -1
    s = str(d).strip().lower()
    if s in ("up", "+", "rose", "higher", "increase", "increased"):
        return 1
    if s in ("down", "-", "fell", "lower", "decrease", "decreased"):
        return -1
    return None


@dataclass
class Fact:
    """One figure the model was handed, with what kind of figure it is.

    kind: measured | computed | estimate | opportunity | projection | plan |
    forecast | benchmark | price | prediction (an unknown kind falls back to
    kind_of_key).
    unit: '$' | '%' | 'pts' | '★' | 'count' | 'h' | 'x' | '' (untyped: backs
    money and counts only, never a % — F8). period: night | day | week |
    month | year | period | None. direction: 'up' / 'down' / ±1 where the
    data says which way it moved. data_days: the days of data behind a
    rate. source: for a benchmark {source, year, source_kind, cohort_label,
    n, min_n, as_of, restaurant_category, engine_kind, strength_pct,
    standing, comparable} (intelligence.engine.facts /
    benchmark_registry.facts); for a prediction the P2 shape in the module
    docstring; for a delivered result "outcomes". A value of None is a fact that was not
    measured — it can never back a figure (null is never 0)."""
    key: str
    value: object = None
    unit: str = ""
    kind: str = ""
    period: object = None
    entity: object = None
    direction: object = None
    as_of: object = None
    data_days: object = None
    source: object = None

    def __post_init__(self):
        self.key = str(self.key or "")
        v = self.value
        if isinstance(v, bool) or v is None:
            self.value = None
        else:
            try:
                self.value = float(v)
                if self.value != self.value:      # NaN
                    self.value = None
            except (TypeError, ValueError):
                self.value = None
        u = str(self.unit or "").strip().lower()
        self.unit = _UNIT_NORM.get(u, _UNIT_NORM.get(str(self.unit or "").strip(), u))
        k = str(self.kind or "").strip().lower()
        self.kind = k if k in FACT_KINDS else kind_of_key(self.key)
        p = str(self.period or "").strip().lower() or None
        self.period = _PERIOD_NORM.get(p, p) if p else None
        self.direction = _norm_direction(self.direction)
        self.entity = (" ".join(str(self.entity).split()) or None) if self.entity not in (None, "") else None
        try:
            self.data_days = int(self.data_days) if self.data_days is not None else None
        except (TypeError, ValueError):
            self.data_days = None


def _as_fact(f):
    if isinstance(f, Fact):
        return f
    if isinstance(f, dict):
        known = {k: f.get(k) for k in ("key", "value", "unit", "kind", "period", "entity", "direction",
                                       "as_of", "data_days", "source")}
        return Fact(**known)
    raise TypeError(f"not a fact: {f!r}")


@dataclass
class ValidationContext:
    """Everything a validation reads. Only `surface` is required; every
    other field narrows what can be checked (no facts and no context_text
    means no figure check — it never guesses a source)."""
    restaurant_id: object = None
    surface: str = "ask"
    audience: str = "owner"
    delivery: str = ""
    facts: list = field(default_factory=list)
    untrusted: list = field(default_factory=list)
    cause_anchors: list = field(default_factory=list)
    names_allowed: set = field(default_factory=set)
    tenant_names_denied: set = field(default_factory=set)
    never_say: list = field(default_factory=list)
    offer_source: str = ""
    confidence: object = None
    data_state: dict = field(default_factory=dict)
    policy: dict = field(default_factory=dict)
    context_text: str = ""

    def __post_init__(self):
        if self.surface not in SURFACES:
            raise ValueError(f"unknown surface {self.surface!r}; one of {', '.join(SURFACES)}")
        if not self.audience:
            self.audience = "guest_public" if self.surface in PUBLIC_SURFACES else "owner"
        if self.audience not in AUDIENCES:
            raise ValueError(f"unknown audience {self.audience!r}")
        if not self.delivery:
            self.delivery = "unattended" if self.surface in UNATTENDED_SURFACES else "interactive"
        if self.delivery not in DELIVERIES:
            raise ValueError(f"unknown delivery {self.delivery!r}")
        if self.surface in PUBLIC_SURFACES and self.audience == "owner":
            self.audience = "guest_public"
        self.facts = [_as_fact(f) for f in (self.facts or [])]
        self.untrusted = [str(u) for u in (self.untrusted or []) if u]
        anchors = []
        for a in self.cause_anchors or []:
            if isinstance(a, dict):
                text, strength = a.get("text"), a.get("strength") or "likely"
            else:
                text, strength = a, "likely"
            text = " ".join(str(text or "").split())
            if text:
                anchors.append({"text": text, "strength": strength if strength in _STRENGTH else "likely"})
        self.cause_anchors = anchors
        self.names_allowed = {str(n) for n in (self.names_allowed or ()) if n}
        self.tenant_names_denied = {str(n) for n in (self.tenant_names_denied or ()) if n}
        if isinstance(self.never_say, str):
            self.never_say = [t.strip() for t in self.never_say.split(",") if t.strip()]
        self.never_say = [str(t).strip() for t in (self.never_say or []) if str(t).strip()]
        self.offer_source = str(self.offer_source or "")
        self.data_state = dict(self.data_state or {})
        self.policy = dict(self.policy or {})
        self.context_text = str(self.context_text or "")


@dataclass
class Verdict:
    text: str
    verdict: str
    findings: list
    actions: dict
    version: str = VERSION

    @property
    def codes(self) -> list:
        """The rule codes that fired, in first-seen order."""
        return list(dict.fromkeys(f["rule"] for f in self.findings))

    def text_with_caveats(self, sep: str = "\n") -> str:
        """The text with its caveats appended — for a surface (an email, a
        push) that cannot render actions.caveats beside the text."""
        if not self.text:
            return ""
        cav = [c for c in self.actions.get("caveats") or [] if c]
        return self.text + (sep + " ".join(cav) if cav else "")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["actions"]["rewrites"] = [list(r) for r in d["actions"]["rewrites"]]
        d["codes"] = self.codes
        return d


@dataclass
class LinesResult:
    """validate_lines: the lines that stand (rewritten), one verdict per
    input line, and the lines dropped."""
    lines: list
    verdicts: list
    dropped: list


# ── kinds from key names ────────────────────────────────────────────────────

_KIND_RULES = (
    # A delivered result is measured, whatever else its key says.
    ("measured", re.compile(r"(?:^|[._])(?:delivered|outcomes?|realised|realized)(?:$|[._])")),
    # A comparison of an actual with a plan is arithmetic on a measurement.
    ("computed", re.compile(r"(?:^|[._])(?:vs|delta|change|diff)(?:$|[._])")),
    ("opportunity", re.compile(r"recoverable|opportunit|at_stake|potential|gap|savings(?!_delivered)|available")),
    ("plan", re.compile(r"budget|target|goal|(?:^|[._])plan(?:$|[._]|ned)")),
    ("estimate", re.compile(r"(?:^|[._])est_|estimat")),
    ("prediction", re.compile(r"(?:^|[._])predict")),
    ("projection", re.compile(r"forecast|project")),
    ("benchmark", re.compile(r"benchmark|industry|cohort|(?:^|[._])p(?:25|50|75)(?:$|[._])|peer")),
    ("price", re.compile(r"(?:^|[._])(?:menu_)?price(?:$|[._])")),
)


def kind_of_key(key) -> str:
    """The kind a fact key names when the caller gave none (contract):
    recoverable | opportunit | at_stake | potential | gap → opportunity;
    budget | target | goal | plan → plan; est_ | estimat → estimate;
    forecast | project → projection; else measured. Also: a delivered /
    outcome key is measured, a vs_/delta/change key is computed (actual
    minus comparator), a benchmark / industry / cohort / p25–p75 key is a
    benchmark, a price key is a price."""
    k = str(key or "").lower()
    for kind, pat in _KIND_RULES:
        if pat.search(k):
            return kind
    return "measured"


_UNIT_FROM_KEY = (
    ("pts", re.compile(r"(?:^|[._])(?:pts|points|pp)(?:$|[._])")),
    ("%", re.compile(r"pct|percent|(?:^|[._])(?:rate|share|margin|ratio_pct)(?:$|[._])")),
    ("★", re.compile(r"rating|stars?(?:$|[._])")),
    ("h", re.compile(r"hours|(?:^|[._])(?:hrs|h)(?:$|[._])")),
    ("count", re.compile(r"count|(?:^|[._])(?:n|num|number|reviews|covers|orders|transactions|shifts|items|"
                         r"guests|posts|no_shows|visits)(?:$|[._])")),
    ("$", re.compile(r"dollars|cost|sales|(?:^|[._])(?:net|gross)(?:$|[._])|revenue|waste|saving|budget|spend|"
                     r"premium|recoverable|at_stake|price|amount|usd|ticket|comps|voids|refunds|payroll|value|"
                     r"monthly|weekly|annual|gap")),
)
_PERIOD_FROM_KEY = (
    ("month", re.compile(r"monthly|(?:^|[._])(?:mo|per_month|month)(?:$|[._])")),
    ("week", re.compile(r"weekly|(?:^|[._])(?:wk|per_week)(?:$|[._])")),
    ("year", re.compile(r"annual|yearly|(?:^|[._])(?:yr|per_year)(?:$|[._])")),
    ("night", re.compile(r"nightly|tonight")),
    ("day", re.compile(r"(?:^|[._])(?:daily|per_day)(?:$|[._])")),
)


def _match_map(mapping, key):
    """A caller's kind/unit/period map: exact key, then the key's last
    segment, then any map key ending the key ("recoverable_monthly" maps
    "food.recoverable_monthly")."""
    if not mapping:
        return None
    if key in mapping:
        return mapping[key]
    last = key.split(".")[-1]
    if last in mapping:
        return mapping[last]
    for mk, mv in mapping.items():
        if key.endswith(mk):
            return mv
    return None


MAX_LIST_ITEMS = 50


def facts_from_dict(d, kind_map=None, *, unit_map=None, period_map=None, entity=None, prefix="",
                    data_days=None) -> list:
    """Typed facts from a payload dict. Nested dicts become dotted keys and
    list items dotted indexes ("benchmarks.0.p50" — the bands a tool
    returns as a list were never facts, BM3-3); only numbers (and None — a
    fact that was not measured) are facts; booleans and strings are not.
    A list is walked to its first MAX_LIST_ITEMS items. Kind comes from `kind_map` (exact
    key, last segment or suffix) else kind_of_key; unit and period from
    their maps else the key's words (pct → %, rating → ★, hours → h, a
    count noun → count, a money word → $; monthly → month …)."""
    out = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
            return
        if isinstance(node, (list, tuple)):
            for i, v in enumerate(node[:MAX_LIST_ITEMS]):
                walk(v, f"{path}.{i}" if path else str(i))
            return
        if isinstance(node, bool) or not (node is None or isinstance(node, (int, float))):
            return
        key = path
        low = key.lower()
        kind = _match_map(kind_map, key) or kind_of_key(key)
        unit = _match_map(unit_map, key)
        if unit is None:
            unit = next((u for u, pat in _UNIT_FROM_KEY if pat.search(low)), "")
            if unit == "★" and node is not None and not (1.0 <= float(node) <= 5.0):
                unit = ""
        period = _match_map(period_map, key)
        if period is None:
            period = next((p for p, pat in _PERIOD_FROM_KEY if pat.search(low)), None)
        out.append(Fact(key=key, value=node, unit=unit, kind=kind, period=period, entity=entity,
                        data_days=data_days))

    walk(d or {}, prefix)
    return out


def is_delivered(fact) -> bool:
    """A measured before/after result (outcomes / value_delivered.delivered)
    — the only kind "saved" or "recovered" may describe."""
    f = _as_fact(fact)
    if f.kind != "measured":
        return False
    src = f.source.get("source") if isinstance(f.source, dict) else f.source
    if str(src or "").lower() in ("outcomes", "value_delivered", "delivered"):
        return True
    return bool(re.search(r"(?:^|[._])(?:delivered|outcomes?|realised|realized)(?:$|[._])", f.key.lower()))


# ── confidence ──────────────────────────────────────────────────────────────

def _pct(conf):
    """The K1 percentage, or None when it is not measurable."""
    if conf is None:
        return None
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        return float(conf)
    if isinstance(conf, dict):
        p = conf.get("pct")
        if isinstance(p, (int, float)) and not isinstance(p, bool):
            return float(p)
    return None


# Modal strength: will 4 > should/likely 3 > could/may 2 > might 1.
def target_level(pct) -> int:
    """The strongest modal a K1 % allows (pinned to HIGH_AT / MEDIUM_AT)."""
    if pct is None:
        return 1
    if pct >= HIGH_AT:
        return 3
    if pct >= MEDIUM_AT:
        return 2
    return 1


_MODAL_FOR = {3: "should", 2: "could", 1: "might"}
_STRENGTH = {"supported": 3, "likely": 2, "association": 1}


# ── lexicons ────────────────────────────────────────────────────────────────

_HEDGE_BEFORE_RE = re.compile(
    r"\b(?:may|might|could|possibly|probably|likely|perhaps|partly|in\s+part|appears?\s+to|seems?\s+to|"
    r"suggests?|consistent\s+with|one\s+possible|may\s+well|part\s+of)\b[^.;]{0,30}$", re.I)
_NEGATED_BEFORE_RE = re.compile(r"\b(?:not|n['’]t|no|never|nothing)\b[\w\s,'’-]{0,14}$", re.I)
_MOVE_PAST = (r"(?:rose|fell|dropped|climbed|slipped|declined|jumped|increased|decreased|improved|worsened|eased|"
              r"spiked|dipped|sank|grew|shrank|soared|plunged|tumbled|surged|went\s+(?:up|down)|took\s+a\s+hit|"
              r"complained|got\s+worse|came\s+in|fell\s+short|ran\s+(?:high|over|heavy|hot)|slowed|picked\s+up|"
              r"have\s+gotten\s+(?:worse|better)|has\s+gotten\s+(?:worse|better)|ran|hit|landed|averaged|"
              r"totall?ed|stayed|slid|lagged|trailed)")
_MOVE_PAST_RE = re.compile(r"\b" + _MOVE_PAST + r"\b", re.I)

# Realised money verbs (NS3 R2), losses, and promises about money.
_REALISED_RE = re.compile(
    r"\b(?:saved|have\s+saved|has\s+saved|are\s+saving|is\s+saving|saving\s+you|saves\s+you|save\s+you|"
    r"recovered|recouped|clawed\s+back|earned|made\s+you|delivered|paid\s+off|you\s+kept|avoided|"
    r"savings\s+of|(?:labor|food|waste|overtime|total)\s+savings|(?:largest|biggest|total|a)\s+savings?|"
    r"saving(?=\s+(?:you\s+|us\s+)?(?:about\s+|roughly\s+|around\s+|nearly\s+)?\$))\b", re.I)
# "Measured" said of a figure that is not a measurement (NS1 H4).
_MEASURED_WORD_RE = re.compile(r"\b(?:measured|not\s+an\s+estimate|exact(?:ly)?|actual(?:ly)?)\b", re.I)
_LOSS_RE = re.compile(
    r"\b(?:losing|lost|lose|cost\s+you|costing\s+you|costs\s+you|wasted|wasting|you\s+are\s+losing|"
    r"you['’]re\s+losing|bleeding)\b", re.I)
_PROMISE_RE = re.compile(
    r"\b(?:will|['’]ll|is\s+going\s+to|are\s+going\s+to|guarantee[sd]?\s+to)\s+(?:\w+\s+)?"
    r"(?:save|cut|recover|reduce|free\s+up|put)\b", re.I)
_OPP_HEDGE_RE = re.compile(
    r"\b(?:could|would|potential|opportunit\w*|at\s+stake|on\s+the\s+table|available|if\b|estimated|"
    r"recoverable|gap|above\s+(?:your\s+|the\s+)?target|over\s+(?:your\s+|the\s+)?target|to\s+target|"
    r"up\s+to|possible)", re.I)
_EST_HEDGE_RE = re.compile(
    r"\b(?:estimat\w*|est\.|forecast\w*|projected|projection|expected|about|roughly|around|approximately|"
    r"nearly|almost|if\s+it\s+holds|at\s+this\s+pace|if\s+nothing\s+changes)\b|~", re.I)
# "not money saved" / "not a saving" names what a figure is NOT; blanked
# before the realised-money verbs are read.
_NEGATED_SAVING_RE = re.compile(r"\bnot\s+(?:money\s+|a\s+|any\s+)?(?:saved|savings?|recovered)\b", re.I)
# A saving word inside a hedge — "could be recovered", "can save", "might
# save you" — states a possibility, not money saved (never "could have
# saved", which says it was not). Blanked before the realised verbs are read.
_HEDGED_SAVE_RE = re.compile(r"\b(?:could|can|might|may|would)\s+(?!have\b)(?:\w+\s+){0,2}?"
                             r"(?:sav(?:e|ed|ing)|recover(?:ed)?|recoup(?:ed)?)\b(?:\s+you\b)?", re.I)
# Where one figure's clause ends: a comma or semicolon, a dash, or a joining
# word — "You saved $1,200, and waste was $84.50" puts "saved" on $1,200 only.
_CLAUSE_BOUNDARY_RE = re.compile(r"[,;]\s|\s[—–-]\s|\b(?:and|but|while|whereas)\b", re.I)


def _clause_span(t, start, end) -> tuple:
    """(a, b): the clause of `t` the figure at t[start:end] sits in."""
    a = 0
    for m in _CLAUSE_BOUNDARY_RE.finditer(t, 0, start):
        a = m.end()
    m = _CLAUSE_BOUNDARY_RE.search(t, end)
    return a, (m.start() if m else len(t))
_PROJ_WORD_RE = re.compile(r"\b(?:projection|projected|if\s+it\s+holds|at\s+this\s+pace|if\s+nothing\s+changes)\b",
                           re.I)
_ON_PACE_RE = re.compile(r"\b(?:on\s+pace|on\s+track\s+for|run[- ]rate|annuali[sz]ed|pacing\s+(?:for|toward))\b",
                         re.I)
_SUM_RE = re.compile(r"\b(?:together|combined|in\s+total|add(?:s|ed)?\s+up|altogether|all\s+told|total\s+of|"
                     r"call\s+it|in\s+all|sum\s+of)\b", re.I)
_HEDGE_NEAR_FIGURE_RE = re.compile(r"\b(?:about|roughly|around|approximately|nearly|almost|some|~)\s*$", re.I)

# Canonical labels the engine writes. Blanked before any detection, so a
# second pass never reads its own label as a claim (idempotence).
_OWN_LABEL_RE = re.compile(
    r"\((?:an?\s+)?(?:opportunity|estimate|projection|general\s+industry\s+figure)[^)]*\)|\bfor\s+your\s+OK\b",
    re.I)

_POINT_WORDS = {"points", "point", "pts", "pt", "pp"}
# A prompt's bare numeral that counts time or things — never a % (F8).
_COUNTED_BARE_RE = re.compile(
    r"(?<![\w.$])(\d[\d,]*(?:\.\d+)?)\s+(?:[A-Za-z-]+\s+)?(?:days?|nights?|weeks?|months?|years?|hours?|hrs|"
    r"shifts?|reviews?|reviewers?|guests?|covers?|orders?|items?|posts?|people|staff|employees?|visits?|"
    r"restaurants?|locations?|competitors?|mentions?|complaints?|tables?)\b", re.I)
_HOUR_WORDS = {"hours", "hour", "hrs", "hr", "h"}
_COUNT_NOUNS = set(_g.COUNT_NOUNS) | {"no-shows", "nights", "shifts", "tables"}
_MULT_RE = re.compile(r"(?<![\w.$])(\d+(?:\.\d+)?)\s?(?:x|×|times)\b(?!\s*(?:a|per|each)\s+(?:day|week|month|night|"
                      r"shift|year))", re.I)

_PERIOD_AFTER_RE = re.compile(
    r"^\s*(?:(?:a|an|per|each|every|this|last|that|the|over\s+(?:a|the)|for\s+the|/)\s*)?"
    r"(?:(?:past|prior|previous|whole|full)\s+)?"
    r"(week|wk|month|mo|year|yr|day|night|shift|weekly|monthly|yearly|annually|annual|daily|nightly|tonight|"
    r"today|period)\b", re.I)
_PERIOD_NEAR_AFTER_RE = re.compile(
    r"^((?:\s+[\w'’-]+){0,3}?)\s+(tonight|today|this\s+(?:week|month|year|period)|last\s+(?:week|month|year|night)|"
    r"(?:a|per|every|each)\s+(?:week|month|year|night|day))\b", re.I)
# "$909 ahead of last week": the period there is the comparator, not the
# figure's own.
_COMPARATOR_WORDS = {"of", "than", "vs", "versus", "from", "behind", "ahead", "over", "compared", "against", "below",
                     "above", "under", "since", "on", "to"}
_PERIOD_BEFORE_RE = re.compile(
    r"\b(weekly|monthly|annual|yearly|nightly|daily)\s+(?:[\w-]+\s+){0,2}?(?:of\s+|cost:?\s+|is\s+|are\s+|:\s*)?$",
    re.I)
_RATE_WORDS = {"a", "an", "per", "each", "every", "/", "over"}


def _period_of(sentence: str, claim) -> tuple:
    """(period, is_rate) a money figure is stated for, or (None, False).
    A rate ("a month", "per year", "/mo", "monthly …") is recurring; a
    deictic ("tonight", "this week", "last month") names one period."""
    after = sentence[claim["end"]:claim["end"] + 40]
    m = _PERIOD_AFTER_RE.match(after)
    if m:
        word = m.group(1).lower()
        lead = after[:m.start(1)].strip().lower().split()
        p = _PERIOD_NORM.get(word, word)
        rate = bool(lead and lead[0] in _RATE_WORDS) or bool(lead and lead[0].startswith("/")) or \
            word in ("weekly", "monthly", "yearly", "annually", "annual", "daily", "nightly") or \
            after.lstrip().startswith("/")
        if word in ("tonight", "today"):
            rate = False
        return p, rate
    m = _PERIOD_NEAR_AFTER_RE.match(after)
    if m and not (set(m.group(1).lower().split()) & (_COMPARATOR_WORDS - {"on"})):
        words = m.group(2).lower().split()
        p = _PERIOD_NORM.get(words[-1], words[-1])
        return p, words[0] in ("a", "per", "every", "each")
    before = sentence[max(0, claim["start"] - 40):claim["start"]]
    m = _PERIOD_BEFORE_RE.search(before)
    if m:
        return _PERIOD_NORM.get(m.group(1).lower(), m.group(1).lower()), True
    m = re.search(r"\b(?:over|in|for|per)\s+(?:a|one|the)\s+(year|month|week)\b[^.$]{0,20}$", before, re.I)
    if m:
        return m.group(1).lower(), True
    return None, False


_SCALE = {("week", "month"): (52 / 12, 4.33, 4.3, 4.345, 4.35), ("month", "year"): (12,),
          ("week", "year"): (52,), ("night", "month"): (30, 30.44, 31), ("day", "month"): (30, 30.44, 31),
          ("night", "week"): (7,), ("day", "week"): (7,), ("day", "year"): (365,), ("night", "year"): (365,),
          ("period", "month"): (), ("period", "year"): ()}


def _compatible(stated, fact_period):
    if stated == fact_period:
        return True
    return {stated, fact_period} <= {"night", "day"}


# ── K1: causal connectives ──────────────────────────────────────────────────
#
# (pattern, level, forward, needs_move, rewrite_to_likely, rewrite_to_association)
# level: 3 states a cause, 2 a contribution, 1 a relationship. A forward
# connective puts the cause after it ("fell because X"); a backward one
# before it ("X caused …"). needs_move: temporal words ("since", "when",
# "after", "following", "once", "with") are causal only beside a past-tense
# movement. The rewrites return the replacement for the matched text, or
# None when no honest rewrite exists (the finding is then a caveat).

def _tense(t):
    t = t.lower()
    if re.match(r"(?:is|are|am)\s", t):
        return "present"
    if re.search(r"\b(?:was|were|been)\b|ed\b|\bhurt\b|\bdrove\b|\bled\b|\bhad\b|\bpaid\b|\bbrought\b", t):
        return "past"
    if re.search(r"ing\b", t):
        return "ing"
    return "present"


def _by_tense(past, present, ing=None):
    return lambda t, rest: {"past": past, "present": present, "ing": ing or present}[_tense(t)]


def _is_noun_phrase(rest):
    """Whether what follows a connective is a noun phrase ("after the menu
    change") rather than a clause ("after the new manager started")."""
    head = re.split(r"[,.;:!?—]", rest, maxsplit=1)[0]
    if not re.match(r"\s*(?:the|a|an|your|this|that|last|its|their|our)\b", head, re.I):
        return False
    return not re.search(r"\b(?:\w{3,}ed|began|went|came|left|took|made|cut|ran|started|opened|closed|is|are|was|"
                         r"were|has|have|had)\b", head, re.I)


def _around(t, rest):
    return "around the time of" if _is_noun_phrase(rest) else "around the time"


def _possibly_because(t, rest):
    return "possibly because of" if _is_noun_phrase(rest) else "possibly because"


# "caused food cost to jump": the object then an infinitive.
_X_TO_VERB_RE = re.compile(r"^\s+(?:[\w'’%.-]+\s+){1,4}?to\s+[a-z]+\b", re.I)


def _copula_swap(mapping):
    def f(t, rest):
        cop = t.split()[0].lower()
        return mapping.get(cop)
    return f


_CAUSAL = [
    # forward
    (r"because\s+of", 3, True, False, "possibly because of", "alongside"),
    (r"because", 3, True, False, "possibly because", "while"),
    (r"due\s+to", 3, True, False, "possibly due to", "alongside"),
    (r"(?:is|are|was|were)\s+caused\s+by", 3, True, False,
     _copula_swap({"is": "may partly reflect", "are": "may partly reflect", "was": "may partly reflect",
                   "were": "may partly reflect"}),
     _copula_swap({"is": "coincides with", "are": "coincide with", "was": "coincided with",
                   "were": "coincided with"})),
    (r"caused\s+by", 3, True, False, "possibly caused by", "alongside"),
    (r"driven\s+by", 3, True, False, "possibly driven by", "alongside"),
    (r"thanks\s+to", 3, True, False, "possibly helped by", "alongside"),
    (r"as\s+a\s+result\s+of|(?<!as\s)(?<!a\s)result\s+of", 3, True, False, "possibly as a result of", "alongside"),
    (r"owing\s+to|on\s+account\s+of", 3, True, False, "possibly owing to", "alongside"),
    (r"stem(?:s|med|ming)?\s+from", 3, True, False, "may stem from", None),
    (r"attribut(?:able|ed)\s+to", 3, True, False, "possibly attributable to", "alongside"),
    (r"fu(?:e)?l(?:l)?ed\s+by", 3, True, False, "possibly fuelled by", "alongside"),
    # "Most of the waste came from salmon" is composition, not a cause.
    (r"(?:came|comes)\s+from", 3, True, False, "may have come from", None),
    (r"(?<!\bif\s)(?:the\s+)?(?:main\s+|real\s+|root\s+|likely\s+)?cause\s+(?:is|was|:)"
     r"(?!\s+(?:right|correct|confirmed|wrong|unclear|unknown|not|still))", 3, True, False,
     lambda t, rest: "a possible cause " + ("was" if t.lower().rstrip().endswith("was") else "is"), None),
    (r"the\s+reason\s+(?:[\w'’]+\s+){0,5}?(?:is|was)", 3, True, False,
     lambda t, rest: re.sub(r"^the\s+reason", "one possible reason", t, flags=re.I), None),
    (r"(?<![\w-])since(?!\s+(?:last|this|yesterday|then|mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|"
     r"aug|sep|oct|nov|dec|\d|the\s+(?:start|beginning|first|last|week|month|year)|we\s+opened|you\s+opened|"
     r"opening|launch))", 3, True, True, "possibly because", "in the same period that"),
    (r"when", 3, True, True, "possibly because", "around the time"),
    (r"once", 3, True, True, "possibly because", "around the time"),
    (r"following", 3, True, True, _possibly_because, "alongside"),
    (r"after", 3, True, True, _possibly_because, _around),
    (r"^with(?=[^,]{1,60},)", 3, True, True, "possibly because of", "alongside"),
    (r"as\s+a\s+result(?=\s*,)", 3, True, False, "possibly as a result", "at the same time"),
    (r"blame", 3, True, False, None, None),
    # backward
    (r"caus(?:ed|es|ing)(?!\s+by)(?!\s+(?:is|was)\b)", 3, False, False,
     lambda t, rest: ("may have helped cause" if _X_TO_VERB_RE.match(rest) else
                      _by_tense("may have contributed to", "may be contributing to", "possibly contributing to")(t, rest)),
     lambda t, rest: (None if _X_TO_VERB_RE.match(rest) else
                      _by_tense("coincided with", "coincides with", "alongside")(t, rest))),
    (r"led\s+to|leads\s+to|leading\s+to", 3, False, False,
     _by_tense("may have contributed to", "may be contributing to", "possibly contributing to"),
     _by_tense("coincided with", "coincides with", "alongside")),
    (r"result(?:ed|s|ing)\s+in", 3, False, False,
     _by_tense("may have contributed to", "may be contributing to", "possibly contributing to"),
     _by_tense("coincided with", "coincides with", "alongside")),
    (r"(?:is\s+|are\s+)?(?:drove|drives|driving)", 3, False, False,
     _by_tense("may have driven", "may be driving", "possibly driving"),
     _by_tense("moved with", "moves with", "moving with")),
    (r"trigger(?:ed|s)", 3, False, False, _by_tense("may have contributed to", "may be contributing to"),
     _by_tense("coincided with", "coincides with")),
    (r"lift(?:ed|s)(?!\s+(?:up|off))", 3, False, False, _by_tense("may have lifted", "may be lifting"), None),
    (r"(?:is\s+|are\s+)?pa(?:ying|ys)\s+for\s+itself", 3, False, False, "may be paying for itself", None),
    (r"(?:is\s+|are\s+)?hurt(?:s|ing)?", 3, False, False,
     _by_tense("may have hurt", "may be hurting", "possibly hurting"),
     _by_tense("coincided with", "coincides with", "alongside")),
    (r"boost(?:ed|s)", 3, False, False, _by_tense("may have boosted", "may be boosting"),
     _by_tense("coincided with", "coincides with")),
    (r"fu(?:e)?l(?:l)?ed(?!\s+by)", 3, False, False, "may have fuelled", "coincided with"),
    (r"(?:is|are|was|were|be|been)\s+(?:the\s+|one\s+)?(?:main\s+|real\s+)?reasons?", 3, False, False,
     lambda t, rest: "may be part of the reason", None),
    (r"(?:is|are|was|were)\s+(?:the\s+|a\s+)?(?:main\s+|real\s+|root\s+|biggest\s+|likely\s+)?"
     r"(?:cause|driver|culprit)(?!\s+(?:is|was|:))", 3, False, False,
     lambda t, rest: "may be a " + t.split()[-1].lower(), None),
    (r"(?:which\s+is|that['’]?s|that\s+is|this\s+is|it['’]?s|is|was)\s+why", 3, False, False,
     lambda t, rest: ("which may be part of why" if t.lower().startswith("which") else
                      "that may be part of why" if t.lower().startswith(("that", "this", "it")) else
                      "may be part of why"), None),
    (r"(?:is|are|was|were|be|been)\s+behind", 3, False, False,
     _copula_swap({"is": "may be behind", "are": "may be behind", "was": "may have been behind",
                   "were": "may have been behind"}),
     _copula_swap({"is": "coincides with", "are": "coincide with", "was": "coincided with",
                   "were": "coincided with"})),
    (r"(?:is|are|was|were|be|been)\s+responsible\s+for", 3, False, False,
     _copula_swap({"is": "may be contributing to", "are": "may be contributing to",
                   "was": "may have contributed to", "were": "may have contributed to"}),
     _copula_swap({"is": "coincides with", "are": "coincide with", "was": "coincided with",
                   "were": "coincided with"})),
    (r"(?:is|are|was|were|be)\s+to\s+blame(?:\s+for)?", 3, False, False,
     lambda t, rest: "may be part of the reason" + (" for" if t.lower().endswith("for") else ""), None),
    (r"explain(?:s|ed)(?!\s+(?:how|what|the\s+(?:rule|steps)))", 3, False, False,
     _by_tense("may have helped explain", "may help explain"), None),
    (r"contribut(?:ed|es|ing)\s+to", 2, False, False, None,
     _by_tense("coincided with", "coincides with", "alongside")),
    (r"play(?:ed|s|ing)?\s+(?:a|an)\s+(?:\w+\s+)?(?:part|role)\s+in", 2, False, False, None, "coincided with"),
    (r"paid\s+off|pays\s+off|paying\s+off", 3, False, False, _by_tense("may have paid off", "may be paying off"),
     None),
    (r"(?:is|are|was|were)\s+(?:directly\s+|closely\s+)?(?:tied|linked|related|connected)\s+to|"
     r"(?:is|are|was|were)\s+associated\s+with", 1, False, False, None,
     _copula_swap({"is": "coincides with", "are": "coincide with", "was": "coincided with",
                   "were": "coincided with"})),
    (r"(?:tied|linked|related|connected)\s+to", 1, False, False, None, None),
    (r"ha(?:d|s|ve)\s+(?:a|an)\s+(?:\w+\s+){0,2}?(?:impact|effect)\s+on|impacted|affected", 3, False, False,
     _by_tense("may have affected", "may be affecting"), _by_tense("coincided with", "coincides with")),
    (r"means(?=\s+(?:guests|you|your|the|that|customers|people|we|more|fewer|less))", 3, False, False,
     "may mean", None),
    (r",\s*so(?=\s+(?:\w+\s+){0,3}?(?:rated|fell|rose|dropped|went|came|stayed|spent|complained|left|ordered|lost|"
     r"gained|slipped|climbed|declined)\b)", 3, False, False, None, ", and"),
    (r"worked(?=\s*[:—–-])", 3, False, False, "may have worked", None),
    (r"(?:is\s+|are\s+)?(?:bringing|brought)\s+in", 3, False, False,
     _by_tense("may have brought in", "may be bringing in", "may be bringing in"), None),
]
_CAUSAL_RE = re.compile(
    "|".join(f"(?P<c{i}>" + (p if p.startswith(("^", ",")) else r"(?<!\w)" + p) + r"(?!\w))"
             for i, (p, *_rest) in enumerate(_CAUSAL)), re.I)

# Every connective above contains one of these words. The full pattern is
# costly (an alternation of ~50 phrases, each behind a lookbehind), so it
# only runs from just before a hint (a connective starts at most ~45
# characters before its keyword: "the reason reviews dropped is").
_CAUSAL_HINT_RE = re.compile(
    r"\b(?:because|due|caus|driven|thanks|result|owing|account|stem|attribut|fuel|fule|full|came|comes|reason|"
    r"since|when|once|following|after|with|blame|led|lead|drov|driv|trigger|hurt|boost|why|behind|responsib|"
    r"explain|contribut|play|paid|pays|paying|tied|linked|related|connected|associated|impact|effect|affected|"
    r"means|so|worked|bring|brought|lift)", re.I)

# Honest co-movement wording: never a claim (NS6 §D "moved together").
_NEGATED_ANCHOR_RE = re.compile(r"^\s*NOT\s+a\s+cause|\bnot\b.{0,30}\bcaus", re.I)


# ── C1: certainty ───────────────────────────────────────────────────────────

_OUTCOME_VERBS = (r"save|cut|reduce|lift|increase|boost|recover|keep|fix|win|eliminate|improve|raise|lower|double|"
                  r"triple|fill|bring|grow|pay|hit|close|drive|return|prevent|stop|lose|cost|run|stay|pull|push|"
                  r"protect|drop|rise|fall|climb|go|make|get|end|clear|beat|solve|turn")
_WILL_RE = re.compile(
    r"(?P<will>\bwill\b|['’]ll\b|\b(?:is|are|am)\s+going\s+to\b)\s+(?P<adv>(?:\w+ly\s+)?)(?P<neg>not\s+)?"
    r"(?=(?:" + _OUTCOME_VERBS + r")\w*\b)", re.I)
_WONT_RE = re.compile(r"\bwon['’]t\s+(?=(?:" + _OUTCOME_VERBS + r")\w*\b)", re.I)
_SHOULD_RE = re.compile(r"(?<!\byou\s)(?<!\bwe\s)(?<!\bI\s)\bshould\s+(?=(?:" + _OUTCOME_VERBS + r")\w*\b)", re.I)
_LIKELY_TO_RE = re.compile(r"\b(?:is|are|was|were)\s+(?:very\s+|highly\s+|most\s+|quite\s+)?likely\s+to\s+", re.I)
_LIKELY_BE_RE = re.compile(r"\b(?:is|are)\s+(?:almost\s+certainly|very\s+likely|highly\s+likely|most\s+likely|"
                           r"likely)\b(?!\s+to\b)", re.I)
_CONDITIONAL_RE = re.compile(r"\b(?:if|unless|could|might|may|likely|estimat\w*|forecast\w*|projected|projection|"
                             r"expect\w*|should)\b", re.I)
_ALWAYS_RE = re.compile(
    r"(?<!\bas\s)\b(always|never)\s+(?=(?:run|complain|fill|beat|happen|work|wait|come|rate|sell|miss|win|lose|go|"
    r"get|show|slow|see|respond|return|order|spend|book|leave|pay|drop|rise|fall|stay|hit|improve|love|notice|"
    r"back|respond|fail|pan|pull|bring|sell|draw|pack|mention|like|want)\w*\b)", re.I)
_EVERY_TIME_RE = re.compile(r"^every\s+time\b", re.I)

_BANNED = [
    # (pattern, replacement by target level {3,2,1} or a fixed string)
    (re.compile(r"\b(?:is|are|was|were)\s+guaranteed\s+to\b", re.I), {3: "should", 2: "could", 1: "might"}),
    (re.compile(r",?\s*guaranteed(?=\s*[.!?]?\s*$)", re.I), ""),
    (re.compile(r"\bguarantees\s+(?:that\s+)?", re.I), {3: "should help ", 2: "could help ", 1: "might help "}),
    (re.compile(r"\bI\s+guarantee\b", re.I), "I expect"),
    (re.compile(r"\bguaranteed\s+", re.I), ""),
    (re.compile(r"\bguarantee[ds]?\b", re.I), "expect"),
    (re.compile(r"\b(?:definitely|certainly|undoubtedly|unquestionably|absolutely|without\s+(?:a|any)\s+doubt|"
                r"no\s+doubt|for\s+sure|clearly)\b,?\s*", re.I), ""),
    (re.compile(r"\bproves\b", re.I), "suggests"),
    (re.compile(r"\bprove\b", re.I), "suggest"),
    (re.compile(r"\bproven\b", re.I), "suggested"),
    (re.compile(r"\bis\s+proof(?:\s+that)?\b(?!\s+of\b)", re.I), "may be a sign"),
    (re.compile(r"(?<!\bnot\s)(?<!\bno\s)\bproof\s+(?:that\s+)?(?=(?:the|your|this|it|we|you|our|cavnar)\b)", re.I),
     "a sign that "),
    (re.compile(r"\bensures?\b", re.I), "supports"),
    (re.compile(r"\b100\s?%\s+(?:sure|certain|confident|guaranteed)\b", re.I), ""),
    (re.compile(r"\b100\s?%\s+of\s+the\s+time\b", re.I), "often"),
]


# ── A1 / A2 / P1 / B1 / T1 / I1 lexicons ────────────────────────────────────

_ACTION_CLAIM_RE = re.compile(
    r"\b(?P<who>I['’]ve|I\s+have|I|we['’]ve|we\s+have|we)\s+(?:already\s+|just\s+)?"
    r"(?P<verb>sent|posted|ordered|texted|published|emailed|messaged|submitted|placed|booked|called|paid|"
    r"replied|scheduled\s+the\s+(?:post|text|email)|approved)\b", re.I)
_DISCIPLINE_RE = re.compile(
    r"\b(?i:fire|terminate|discipline|suspend|reprimand|demote|write\s+up|dock)\s+(?i:the\s+|your\s+|our\s+)?"
    r"(?:[\w'’]+\s+){0,2}?(?:[A-Z][a-z]{2,}\b|closers?|openers?|servers?|cooks?|bartenders?|hosts?|managers?|staff|"
    r"employees?|bussers?|dishwashers?|chefs?|waiters?|waitress(?:es)?|runners?)\b"
    r"|\blet\s+(?:[A-Z][a-z]{2,}|the\s+\w+|your\s+\w+)\s+go\b", re.UNICODE)
_FOOD_SAFETY_RE = re.compile(
    r"\b(?:skip(?:ping)?\s+(?:the\s+)?(?:temp(?:erature)?\s+(?:logs?|checks?)|sanitiz\w+|hand-?washing|cooling\s+logs?)"
    r"|extend(?:ing)?\s+(?:the\s+)?(?:hold(?:ing)?\s+times?|shelf[- ]life)"
    r"|serve\s+(?:[\w'’]+\s+){0,3}?(?:past|after)\s+(?:its|the|their)\s+(?:date|use[- ]by|expiration|sell[- ]by)"
    r"|(?:use|serve)\s+(?:it\s+|them\s+)?anyway|re-?freez\w+|leave\s+(?:it\s+|them\s+)?out\s+overnight"
    r"|cool\s+(?:it\s+|them\s+)?at\s+room\s+temperature"
    r"|(?:use|keep\s+using)\s+(?:it\s+|them\s+)?(?:past|after)\s+(?:its|the|their)\s+(?:date|expiration|use[- ]by))\b",
    re.I)
_ROLE_WORDS = r"(?:cooks?|servers?|bartenders?|hosts?|bussers?|dishwashers?|runners?|managers?|barbacks?|expos?|closers?)"
_CUT_TO_RE = re.compile(
    r"\b(?:cut|trim|drop|go|down|bring\s+(?:it|them)|run)\s+(?:down\s+)?to\s+(?P<n>\d+|one|two|three|four|a\s+single|"
    r"a\s+lone|just\s+one|only\s+one|zero|no)\s+(?P<role>" + _ROLE_WORDS + r")\b", re.I)
_SEND_HOME_RE = re.compile(
    r"\b(?i:send|let)\s+(?P<who>[A-Z][a-z]{2,}|(?i:the\s+(?:only|last|lone)\s+(?:closer|keyholder|manager)))\s+"
    r"(?:\w+\s+){0,2}?(?i:home)\b|\b(?i:cut|send\s+home)\s+(?P<who2>[A-Z][a-z]{2,})\b", re.UNICODE)
_LEGAL_FLAT_RE = re.compile(
    r"\b(?:that['’]?s|this\s+is|it['’]?s|it\s+is|you['’]?re|you\s+are|we['’]?re|we\s+are|is|are)\s+"
    r"(?:(?:fully|perfectly|totally|completely|100%)\s+)?(?:legal|compliant|lawful)\b"
    r"|\bno\s+law\s+against\b|\bcompl(?:y|ies)\s+with\s+(?:the\s+|all\s+)?(?:law|laws|rules|regulations|ordinance)\b"
    r"|\bfully\s+compliant\b", re.I)
# The fewest people a cut may leave in a role with no floor set, when the
# caller's policy carries no cut_floor_default of its own. Mirrors the
# restaurants.cut_floor_default column default (schedule_rules.cut_floor).
CUT_FLOOR_DEFAULT = 2
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "a single": 1, "a lone": 1, "just one": 1,
              "only one": 1, "zero": 0, "no": 0}

_P1 = [
    ("allergen or free-from claim", re.compile(
        r"\b(?:nut[- ]free|peanut[- ]free|gluten[- ]free|dairy[- ]free|allergen[- ]free|allergy[- ](?:friendly|safe)|"
        r"celiac[- ](?:safe|friendly)|safe\s+for\s+(?:celiacs?|people\s+with|anyone\s+with|allergies|those\s+with|"
        r"your\s+allerg\w+|kids\s+with)|no\s+cross[- ]contact|free[- ]from)\b", re.I), True),
    ("fault or responsibility admission", re.compile(
        r"\b(?:(?:entirely|completely|totally|all)\s+(?:our|my)\s+fault|(?:our|my)\s+fault|take\s+(?:full\s+|complete\s+)?"
        r"responsibility|we\s+are\s+(?:liable|responsible)|made\s+you\s+(?:ill|sick)|food\s+poisoning)\b", re.I), False),
    ("inspection or compliance claim", re.compile(
        r"\b(?:health\s+inspection|passed\s+(?:our|the|a|every)\s+\w*\s*inspection|(?:fully\s+)?compliant|"
        r"(?-i:\bADA\b)|up\s+to\s+code|health\s+code|certified\s+(?:kitchen|safe))\b", re.I), False),
    ("comp or discount offer", re.compile(
        r"\b(?:(?:dinner|lunch|brunch|drinks?|round|dessert|meal|coffee|appetizer|glass|next\s+\w+|it|that|this)"
        r"(?:\s+is|\s+are|\s+will\s+be|['’]s)?\s+on\s+(?:me|us)|on\s+the\s+house|\d{1,3}\s?(?:%|percent)\s+off|buy\s+you\s+a\s+(?:drink|round|coffee|"
        r"dessert)|(?:is|are|will\s+be)\s+free|free\s+(?:drink|dessert|meal|round|glass|appetizer|entr[eé]e|coffee|"
        r"dinner|lunch|brunch)|complimentary|bogo|buy\s+one,?\s+get)\b", re.I), True),
    ("promise the restaurant never made", re.compile(
        r"\b(?:has\s+been\s+(?:fixed|addressed|resolved)|made\s+sure|won['’]t\s+happen\s+again|will\s+not\s+happen\s+"
        r"again|never\s+happen\s+again|make\s+(?:it|this|things)\s+right|guarantee\w*)\b", re.I), False),
    ("award or ranking claim", re.compile(
        r"(?:\bvoted\b|\baward[- ]winning\b|\bbest\s+(?:\w+\s+){0,2}in\s+(?:town|the\s+city|chicago|[A-Z]\w+)|"
        r"#\s?1\b|\bnumber\s+one\b|\bno\.\s?1\b|\bworld[- ]famous\b|\bfamous\b|\brated\s+\d(?:\.\d)?\s+stars?\s+by)",
        re.I), True),
    ("preparation or sourcing claim", re.compile(
        r"\b(?:made\s+fresh|fresh\s+daily|made\s+from\s+scratch|from\s+scratch|locally[- ]sourced|local,?\s+organic|"
        r"organic|house-?made|homemade|all[- ]natural|farm[- ]to[- ]table|never\s+frozen|trained\s+in\s+[A-Z]\w+)\b",
        re.I), True),
    ("private guest detail", re.compile(r"\b(?:table\s+\d+|your\s+usual\b|your\s+(?:\d{1,2}(?::\d{2})?\s?[ap]m\s+)?"
                                        r"(?:\w+\s+)?reservation)\b", re.I), False),
]

# B1's grammar (BM3-1: a phrase list let 9 of 9 probe phrasings through).
# A group of OTHER businesses: the nouns, and up to two words describing
# them ("most sports bars", "comparable full-service restaurants").
_GROUP_NOUNS = (r"(?:restaurants?|caf[eé]s|coffee\s+shops|bars|taverns|pubs|spots|places|operators|owners|kitchens|"
                r"concepts|businesses|shops|pizzerias|diners|eateries|steakhouses|taquerias|bistros|breweries|"
                r"brewpubs|competitors|venues|joints|chains|establishments)")
# After "other", the nouns that cannot also mean a slot on a schedule or
# a menu ("fill the other spots Friday").
_OTHER_NOUNS = (r"(?:restaurants?|caf[eé]s|coffee\s+shops|bars|taverns|pubs|operators|owners|kitchens|businesses|"
                r"pizzerias|diners|eateries|steakhouses|taquerias|bistros|breweries|brewpubs|competitors|venues|"
                r"chains|establishments)")
_MOD = r"(?:[\w'’-]+\s+){0,2}?"
# A rank or position among others. Never "the top 20% of your dishes" or
# "the bottom half of the schedule": this restaurant's own rankings.
_RANK = (r"(?:top|bottom|upper|lower)\s+(?:quartile|quarter|half|third|decile|\d{1,2}\s?%)"
         r"(?!\s+of\s+(?:your|its|our|the\s+(?:menu|list|week|month|day|night|schedule|shift|year))\b)"
         r"(?!\s+(?:dish|item|seller|server|shift|day|night|hour|product|staff|employee|review|month|week)s?\b)")
_OWN_WINDOW = (r"(?!\s+(?:your|its|this\s+restaurant['’]?s?|our)\s+(?:own|last|past|previous|prior|usual|normal|"
               r"typical|recent|trailing)\b)(?!\s+(?:the\s+)?(?:last|past|previous|prior|trailing|same)\b)")
_BENCH_RE = re.compile(
    r"\b(?:industry\s+(?:average|standard|norm|target|benchmark|range|median|band|figure|data|numbers?)|"
    r"(?:the\s+)?industry['’]s|"
    r"most\s+" + _MOD + _GROUP_NOUNS + r"|"
    r"(?:the\s+)?typical(?:ly)?\s+" + _MOD + _GROUP_NOUNS + r"|"
    r"(?:similar|comparable|peer|leading)\s+" + _MOD + _GROUP_NOUNS + r"|"
    r"other\s+" + _MOD + _OTHER_NOUNS + r"|"
    r"(?:" + _GROUP_NOUNS + r"\s+)?like\s+yours|(?:similar|comparable)\s+to\s+(?:yours|you)|"
    r"area\s+average|(?:the\s+)?average\s+(?:for|in)\s+(?:your|the)\s+area|average\s+restaurant|"
    + _RANK + r"|percentiles?|"
    r"(?:above|below|ahead\s+of|behind|under|over)\s+(?:the\s+)?(?:median|middle|midpoint|average)\s+"
    r"(?:for|of|among|across)\b" + _OWN_WINDOW + r"|"
    r"(?:beats?|beating|outperforms?|outperforming|ahead\s+of|better\s+than|worse\s+than)\s+\d{1,3}\s?%\s+of|"
    r"best[- ]in[- ](?:class|category)|top[- ]performers?|top[- ]performing|top\s+operators|best[- ]run|"
    r"leaders\s+in\s+your|(?:your\s+)?peers|"
    r"nationwide|national\s+average|than\s+most|like\s+most\s+" + _MOD + _GROUP_NOUNS + r"|"
    r"restaurants\s+in\s+your\s+area|area['’]s\s+typical|(?:most|the\s+other)\s+competitors|area\s+restaurants|"
    r"compared\s+(?:to|with)\s+(?:competitors|other\s+restaurants|peers|similar\s+\w+|the\s+area|the\s+industry))"
    r"(?!\w)", re.I)
# "Like yours" and its synonyms: allowed only beside a comparable
# like-for-like peer group (BM4 §4.9).
_LIKE_YOURS_RE = re.compile(
    r"\b(?:(?:(?:similar|comparable|peer)\s+" + _MOD + _GROUP_NOUNS + r")|"
    r"(?:" + _GROUP_NOUNS + r"\s+)?(?:most\s+)?(?:like|similar\s+to|comparable\s+to)\s+(?:yours|you)\b)",
    re.I)
# A ranking word: needs a comparison strong enough to rank on.
_RANK_WORD_RE = re.compile(
    r"\b(?:(?:in\s+)?(?:the\s+)?" + _RANK + r"|percentiles?|(?:well|far|way)\s+(?:above|below|ahead\s+of|behind|under|"
    r"over)|best[- ]in[- ](?:class|category)|leads?\s+(?:the|your)\s+(?:group|cohort|pack))", re.I)
_MEMBER_VALUE_RE = re.compile(r"\b(?:a|one|another)\s+(?:restaurant|caf[eé]|bar|spot|place|kitchen)\s+"
                              r"(?:like\s+yours|nearby|in\s+your\s+(?:area|cohort|group))\b", re.I)
# A Cavnar peer group's floor when the fact names none — mirrors
# intelligence.benchmarks.MIN_QUARTILE_N (pinned by a test; layer 0 can't
# import it). A ranking word needs this comparison strength (a %), the K1
# "high" threshold.
COHORT_MIN_N = 8
STRONG_CLAIM_PCT = HIGH_AT
PLATFORM_CITE = "restaurants on Cavnar, all types"
# The metric a sentence is about, and the metric a benchmark fact is for:
# a claim with no figure binds only to a benchmark for the same metric
# (BM3-5: "your reviews are better than similar restaurants" rode on a
# labor band).
_BENCH_TOPICS = (
    ("labor", r"\blabou?r\b|\bpayroll\b|\bstaffing\b|\bstaff\s+hours\b|\bhours\s+per\b|\bwage", r"labou?r|staff|wage"),
    ("food_cost", r"\bfood[- ]cost|\bcogs\b|\bfood\s+%", r"food_cost|cogs"),
    ("prime_cost", r"\bprime[- ]cost", r"prime"),
    ("waste", r"\bwaste\b|\bwasted\b|\bspoilage\b", r"waste"),
    ("rating", r"\bratings?\b|\bstars?\b|★", r"rating"),
    ("replies", r"\brepl(?:y|ies|ied|ying)\b|\brespon(?:d|ds|se|ses|ding)\b|\banswer(?:s|ed|ing)?\b",
     r"reply|response|respond"),
    ("campaigns", r"\bcampaigns?\b|\btext\s+(?:blasts?|messages?)\b|\btap\s+rate", r"campaign"),
    ("posts", r"\bposts?\b|\bposting\b|\bengagement\b", r"post_"),
    ("outcomes", r"\brecommendations?\b", r"outcomes"),
)
_BENCH_TOPIC_RE = [(k, re.compile(s, re.I), re.compile(f, re.I)) for k, s, f in _BENCH_TOPICS]

# P2: a prediction about similar restaurants.
PREDICTION_MIN_RESTAURANTS = 5
_PREDICTION_RE = re.compile(
    r"\b(?:(?:similar|comparable|peer)\s+" + _MOD + _GROUP_NOUNS + r"|" + _GROUP_NOUNS +
    r"\s+(?:(?:most|very)\s+)?(?:like|similar\s+to|comparable\s+to|close\s+to)\s+(?:yours|you)|"
    r"(?:ones|those)\s+(?:most\s+)?like\s+yours|" + _GROUP_NOUNS + r"\s+with\s+a\s+profile\s+(?:close|similar)\s+to\s+yours)"
    r"\b[^.;]{0,80}?\b(?:reduced|cut|lowered|improved|raised|increased|grew|lifted|boosted|trimmed|dropped|"
    r"brought\s+down|saw\s+[^.;]{0,40}?\b(?:fall|drop|rise|climb|improve|shrink|grow))\b[^.;]{0,60}?"
    r"(?P<n>\d+(?:\.\d+)?)\s?(?:%|percent)", re.I)
_LIKELIHOOD_RE = (
    (re.compile(r"\b(?:an?\s+)?(?:very\s+)?high\s+(?:likelihood|probability|chance)\b", re.I),
     {3: "a good chance", 2: "a fair chance", 1: "some chance"}),
    (re.compile(r"\b(?:almost\s+certainly|very\s+likely|highly\s+likely|most\s+likely)\b", re.I),
     {3: "likely", 2: "possibly", 1: "possibly"}),
    (re.compile(r"(?<!\bun)\blikely\b(?!\s+to\b)", re.I), {3: "likely", 2: "possibly", 1: "possibly"}),
)


# Platforms and products a sentence may name without the input naming them.
_PLATFORM_NAMES = {"instagram", "facebook", "tiktok", "yelp", "google", "opentable", "resy", "tock", "toast",
                   "square", "clover", "cavnar", "chatgpt", "perplexity", "doordash", "ubereats", "grubhub",
                   "apple", "maps", "sysco", "rpower"}


def _norm_name(s):
    return " ".join(str(s or "").replace("’", "'").split()).lower()


# ── helpers ─────────────────────────────────────────────────────────────────

def _cap_like(src, repl):
    if repl and src[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def _tidy(s):
    """Whitespace and punctuation after a phrase was removed."""
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r",\s*([.!?])", r"\1", s)
    s = re.sub(r"^[\s,;:—–-]+", "", s)
    if s[:1].islower() and not re.match(r"^[a-z]+\.", s):
        s = s[:1].upper() + s[1:]
    return s.strip()


def _tidy_line(line):
    """A line after a phrase was removed from it, list marker kept."""
    if not line.strip():
        return line
    m = _LIST_PREFIX_RE.match(line)
    prefix = m.group(1) if m else re.match(r"^\s*", line).group(0)
    body = re.sub(r"^[,;:—–-]+\s*", "", line[len(prefix):])
    # "With high confidence, the …" loses its phrase and leaves "With, the".
    body = re.sub(r"^(?:With|At|In|On)\s*,\s*", "", body)
    if body[:1].islower():
        body = body[:1].upper() + body[1:]
    return prefix + body


def _blank_own(s):
    return _OWN_LABEL_RE.sub(lambda m: " " * len(m.group(0)), s)


def _normalise(s):
    t = _g.normalise_numbers(s)
    t = re.sub(r"\b86(?:['’]?d|['’-]?ed)\b", "eighty-sixed", t, flags=re.I)
    t = re.sub(r"\b\d{1,2}-tops?\b", "tables", t, flags=re.I)
    # "1.2 grand" is $1.2k (NS3 L3). Same length is not needed: rewrites
    # never index this text, only the claims read from it.
    t = re.sub(r"(?<![\w$])(\d+(?:\.\d+)?)\s*grand\b", r"$\1k", t, flags=re.I)
    return t


# A bare number right after a money verb is money: "You saved 1,240".
_MONEY_VERB_BEFORE_RE = re.compile(
    r"\b(?:saved|saving|save|lost|losing|lose|recovered|cost|costing|wasted|wasting|earned|made)\s+"
    r"(?:you\s+|us\s+)?(?:about\s+|roughly\s+|around\s+|nearly\s+|almost\s+)?$", re.I)


def _end_insert(s, suffix):
    """`s` with `suffix` placed before its closing punctuation."""
    m = re.search(r"([.!?]+[\"'’)]*)\s*$", s)
    if m:
        return s[:m.start()] + suffix + s[m.start():]
    return s + suffix


_LIST_PREFIX_RE = re.compile(r"^(\s*(?:\d{1,2}[.)]|[-*•–—]|[A-Z][A-Z /&'’-]{1,30}:)\s+)")
_SPLIT_RE = re.compile(r"(?<=[.!?])(\s+)(?=[A-Z0-9\"'$(])")


def _split_line(line):
    """(prefix, [(sentence, separator)]) — a list marker or a label ("1. ",
    "- ", "ACTION: ") stays out of the sentences."""
    m = _LIST_PREFIX_RE.match(line)
    prefix = m.group(1) if m else re.match(r"^\s*", line).group(0)
    body = line[len(prefix):]
    parts = _SPLIT_RE.split(body)
    out = []
    for i in range(0, len(parts), 2):
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        if parts[i]:
            out.append([parts[i], sep])
    return prefix, out


# ── the fact index ──────────────────────────────────────────────────────────

_GENERIC_KEY_WORDS = {"week", "weekly", "month", "monthly", "year", "yearly", "annual", "night", "nightly", "daily",
                      "count", "total", "value", "dollars", "prev", "last", "change", "delta", "logged", "avg",
                      "rate", "share", "pct", "percent", "points", "target", "budget", "plan", "goal", "hours",
                      "est", "estimated", "forecast", "projection", "opportunity", "potential", "savings", "other",
                      "high", "low", "benchmark", "cohort", "intel"}


def _root(w):
    w = w.lower().strip("'’")
    if w.endswith("'s") or w.endswith("’s"):
        w = w[:-2]
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w

@functools.lru_cache(maxsize=8)
def _context_pool_for(txt: str) -> dict:
    """The prompt text's figures, by unit, for the hybrid fallback — pure in
    the text and cached for the last few (Ask validates one corpus twice).
    The "memo" dict caches lookups, which are pure in (text, query) too."""
    known = _g._figures(txt)
    # A bare numeral that counts days, reviews or shifts ("30 days", "212
    # reviews") never backs a % (F8); any other bare numeral — a JSON dump's
    # "labor_pct": 38.2 — does, as it always did.
    counted = set()
    for m in _COUNTED_BARE_RE.finditer(_g._prepared(txt)):
        try:
            counted.add(round(float(m.group(1).replace(",", "")), 2))
        except ValueError:
            continue
    pct_pool = sorted(set(known["pct"]) | (set(known["bare"]) - counted))[:4000]
    money = _g._money_periods(txt)
    return {"money": money, "money_keys": sorted(money), "bare": sorted(known["bare"]),
            "money_kinds": _context_money_kinds(txt),
            "pct_pool": pct_pool, "dirs": _g._directions(txt), "memo": {}}


# The words beside a money figure in a prompt that say what kind it is — read
# within its own clause, strongest first. "$867 a month above target (an
# opportunity)", "$2,400 recoverable", "a projected $19,850", "budget $4,000".
_CTX_KIND_WORDS = (
    ("opportunity", re.compile(r"\b(?:recoverable|opportunit\w*|at\s+stake|above\s+(?:the\s+)?target|over\s+target|"
                               r"gap|potential|available|not\s+(?:money\s+)?(?:saved|captured)|could\s+save|"
                               r"savings?\s+(?:opportunity|potential|available))\b", re.I)),
    ("projection", re.compile(r"\b(?:project\w*|forecast\w*|on\s+pace|run[- ]rate|if\s+nothing\s+changes|"
                              r"expected|annuali[sz]ed)\b", re.I)),
    ("estimate", re.compile(r"\b(?:estimat\w*|approximat\w*|est\.)", re.I)),
    ("plan", re.compile(r"\b(?:budget\w*|goal|target\s+(?:of|is|:)|planned)\b", re.I)),
)
_CLAUSE_EDGE_RE = re.compile(r"[\n;•]|(?<=[a-z0-9)])\.\s")
_NEIGHBOUR_FIG_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?%")
_COMPARE_EDGE_RE = re.compile(r"\b(?:vs\.?|versus|against|compared|than|and|or|but|while|from)\b|,", re.I)


def _context_money_kinds(txt: str) -> dict:
    """{value: {kind…}} for each money figure the prompt states: the kind its
    own clause names, else measured. A value stated twice with two kinds
    keeps both (then no single kind decides a claim about it)."""
    t = _g._prepared(txt)
    out = {}
    for pat in (_g._MONEY_RE, _g._DOLLARS_RE):
        for m in pat.finditer(t):
            try:
                v = _g._value(m)
            except ValueError:
                continue
            a = max(0, m.start() - 90)
            b = min(len(t), m.end() + 90)
            left = t[a:m.start()]
            right = t[m.end():b]
            edges = list(_CLAUSE_EDGE_RE.finditer(left))
            if edges:
                left = left[edges[-1].end():]
            e = _CLAUSE_EDGE_RE.search(right)
            if e:
                right = right[:e.start()]
            # Only this figure's own words: stop at a neighbouring figure
            # ("sales $5,000 vs expected $4,800" — "expected" is $4,800's).
            prev = list(_NEIGHBOUR_FIG_RE.finditer(left))
            if prev:
                left = left[prev[-1].end():]
            nxt = _NEIGHBOUR_FIG_RE.search(right)
            if nxt:
                right = right[:nxt.start()]
            # ... and on the right, not past a comparison: the words after
            # "vs" / "against" / "than" describe what it is compared with.
            cut = _COMPARE_EDGE_RE.search(right)
            if cut:
                right = right[:cut.start()]
            clause = left + " " + right
            kind = next((k for k, rx in _CTX_KIND_WORDS if rx.search(clause)), "measured")
            out.setdefault(v, set()).add(kind)
    return out


class _Facts:
    def __init__(self, facts):
        self.all = [f for f in facts if f.value is not None]
        self.by_unit = {}
        for f in self.all:
            self.by_unit.setdefault(f.unit, []).append(f)
        self.entities = {}
        for f in self.all:
            if f.entity:
                self.entities.setdefault(f.entity.lower(), []).append(f)
        self._pairs = {}
        self.benchmarks = [f for f in facts if f.kind == "benchmark"]
        # The words a fact is named by, for "does this side of a causal
        # sentence name a fact": the key's words and the entity, rooted;
        # period and bookkeeping words ("week", "count", "prev") name no fact.
        self.words = set()
        for f in facts:
            for w in re.split(r"[_.\W]+", f.key.lower()) + re.split(r"\W+", (f.entity or "").lower()):
                if len(w) >= 4 and w not in _GENERIC_KEY_WORDS:
                    self.words.add(_root(w))

    def units(self, ctype):
        return {"money": ("$", ""), "pct": ("%",), "pts": ("pts",), "star": ("★",), "count": ("count", ""),
                "h": ("h",), "x": ("x",)}[ctype]

    def direct(self, ctype, value, tol, pool=None):
        out = []
        for u in self.units(ctype):
            for f in self.by_unit.get(u, ()):
                if pool is not None and f not in pool:
                    continue
                if abs(abs(value) - abs(f.value)) <= tol:
                    out.append(f)
        return out

    def pairs(self, unit):
        if unit not in self._pairs:
            fs = self.by_unit.get(unit, [])[:40]
            self._pairs[unit] = list(combinations(fs, 2))
        return self._pairs[unit]

    def derived(self, ctype, value, tol):
        """[(facts, how)] of two facts that back the figure: a difference,
        for % a change or share, for x a ratio."""
        out = []
        v = abs(value)
        if ctype in ("money", "count", "h", "star"):
            base = {"money": "$", "count": "count", "h": "h", "star": "★"}[ctype]
            for a, b in self.pairs(base):
                if abs(v - abs(a.value - b.value)) <= tol:
                    out.append(((a, b), "diff"))
        elif ctype == "pts":
            for a, b in self.pairs("%"):
                if abs(v - abs(a.value - b.value)) <= tol:
                    out.append(((a, b), "diff"))
        elif ctype == "pct":
            for u in ("$", "count", "h"):
                for a, b in self.pairs(u):
                    for x, y in ((a, b), (b, a)):
                        if y.value and (abs(v - abs((x.value - y.value) / y.value * 100)) <= tol
                                        or abs(v - abs(x.value / y.value * 100)) <= tol):
                            out.append(((x, y), "change"))
                            break
        elif ctype == "x":
            for u in ("$", "count", "%", "h"):
                for a, b in self.pairs(u):
                    for x, y in ((a, b), (b, a)):
                        if y.value and abs(v - abs(x.value / y.value)) <= max(tol, 0.05 * v):
                            out.append(((x, y), "ratio"))
                            break
        return out

    def sums(self, value, tol):
        """Subsets of 2–3 money facts that add to `value`."""
        fs = [f for f in self.by_unit.get("$", []) if f.kind != "benchmark"][:24]
        out = []
        for n in (2, 3):
            for combo in combinations(fs, n):
                if abs(value - sum(abs(f.value) for f in combo)) <= tol:
                    out.append(combo)
            if out:
                break
        return out


# ── one validation run ──────────────────────────────────────────────────────

class _Run:
    def __init__(self, ctx: ValidationContext):
        self.ctx = ctx
        self.findings, self.caveats, self.rewrites, self.dropped = [], [], [], []
        self.controls = True
        self.unattended = ctx.delivery == "unattended"
        self.public = ctx.audience == "guest_public"
        self.pct = _pct(ctx.confidence)
        self.level = target_level(self.pct)
        self.facts = _Facts(ctx.facts)
        # Typed facts AND the prompt text (the adoption shape, workstream A):
        # the typed facts carry the kinds, periods and entities that matter
        # (money above all); a figure none of them holds may still be one the
        # prompt stated, and is then read as a fact with the old presence
        # semantics — its kind read from the words beside it in the prompt
        # ("$867 a month above target (an opportunity)") — never less strict
        # than the check it replaced. policy["context_facts"] turns the same
        # reading on for a site whose facts are all in its prompt (Ask's
        # snapshot); without it, a prompt-only context keeps the legacy path.
        self.hybrid = bool(ctx.context_text) and not ctx.policy.get("typed_only") and \
            (bool(ctx.facts) or bool(ctx.policy.get("context_facts")))
        self.typed = bool(ctx.facts) or self.hybrid
        self.legacy = (not self.typed) and bool(ctx.context_text)
        self._ctx_pool = None
        anchors = [a for a in ctx.cause_anchors if not _NEGATED_ANCHOR_RE.search(a["text"])]
        if self.public:
            anchors += [{"text": u, "strength": "likely"} for u in ctx.untrusted]
        self.anchors = anchors
        self._name_ctx = None
        self._untrusted_sh = None
        self.denied = []
        allowed = {_norm_name(n) for n in ctx.names_allowed}
        for n in sorted(ctx.tenant_names_denied, key=len, reverse=True):
            nn = _norm_name(n)
            if len(nn) >= 3 and nn not in allowed:
                self.denied.append((n, re.compile(r"(?<![\w])" + re.escape(nn).replace("'", "['’]") + r"(?![\w])",
                                                  re.I)))
        self.unit = ctx.policy.get("unit") or "sentence"
        self._legacy_bad = None
        self._legacy_known = None
        self._body = ""
        self._claims_memo = {}
        self._cur_sentence = None
        self._p2_bound = False
        self._name_known = None
        self.has_delivered = any(is_delivered(f) for f in ctx.facts)
        ents = sorted(self.facts.entities, key=len, reverse=True)
        self.entity_re = (re.compile(r"(?<![\w])(" + "|".join(re.escape(e) for e in ents) + r")(?:s|'s)?(?![\w])",
                                     re.I) if ents else None)
        self.moves = []
        for f in self.facts.all:
            subject = (f.entity or f.key.split(".")[0]).lower()
            if f.direction and len(subject) >= 4:
                self.moves.append((f, subject, re.compile(r"\b" + re.escape(subject) + r"\w*\s+(?:\w+\s+){0,2}?("
                                                          + _MOVE_PAST + r")\b", re.I)))

    # ── findings ──
    def emit(self, rule, sev_i, sev_u=None, span="", detail="", caveat=None):
        sev = (sev_u or sev_i) if self.unattended else sev_i
        if self.public and _SEV_RANK[sev] >= _SEV_RANK["caveat"]:
            sev = "refuse"
        finding = {"rule": rule, "severity": sev, "span": str(span or "")[:60],
                   "detail": str(detail or "")[:200], "action": _ACTION_OF[sev]}
        if self._cur_sentence:
            # The sentence the finding is about, for a client that flags it
            # in place (Ask's unsupported causes). Never logged (log() keeps
            # the span only).
            finding["sentence"] = self._cur_sentence.strip()[:300]
        self.findings.append(finding)
        if sev in ("caveat", "withhold") and caveat and caveat not in self.caveats:
            self.caveats.append(caveat)
        if sev == "withhold":
            self.controls = False
        return sev

    def rewrite(self, rule, src, dst):
        self.rewrites.append((src, dst, rule))
        self.findings.append({"rule": rule, "severity": "rewrite", "span": str(src)[:60],
                              "detail": f"→ {dst}"[:200], "action": "rewrite"})

    # ── context material ──
    @property
    def name_ctx(self):
        if self._name_ctx is None:
            parts = [self.ctx.context_text, " ".join(self.ctx.names_allowed),
                     " ".join(str(k) for k in (self.ctx.policy.get("keyholders") or []))]
            for f in self.ctx.facts:
                parts.append(f.key.replace(".", " ").replace("_", " "))
                if f.entity:
                    parts.append(f.entity)
            parts += [a["text"] for a in self.ctx.cause_anchors]
            self._name_ctx = " ".join(parts)
        return self._name_ctx

    @property
    def untrusted_sh(self):
        if self._untrusted_sh is None:
            sh = set()
            for u in self.ctx.untrusted:
                sh |= _g.shingles(u)
            self._untrusted_sh = sh
        return self._untrusted_sh

    # ── the whole text ──
    def run(self, text):
        ctx = self.ctx
        body = str(text or "")
        if not body.strip():
            if self.public:
                self.emit("P1", "refuse", span="", detail="the text is empty")
            return ""
        if ctx.data_state.get("sample") and not ctx.policy.get("allow_sample"):
            self.emit("M1", "refuse", span="sample data", detail="sample data carries no claims (NS3 R8)")
            return ""
        # C2 first: the model's own confidence, on the whole text (it removes
        # whole sentences). The computed % is shown by the client beside the
        # text; stamping it onto one claim put one answer's overall % on a
        # single causal sentence (NS1 M5), so by default the phrase goes.
        stamp = self.pct if ctx.policy.get("confidence_stamp") else None
        out, n = _g.rewrite_confidence_claims(body, stamp, bands=ctx.policy.get("confidence_bands", True))
        if n:
            self.rewrite("C2", "model-stated confidence", f"{n} phrase(s) removed")
            body = "\n".join(_tidy_line(ln) for ln in out.split("\n"))
        self._body = body
        # Public text is checked whole by the existing public checks.
        if self.public:
            self.public_checks(body)
        # A model-written forecast ("What should change") stands only as a
        # conditional (NS1 H10 / V8): "If the cause is right, …".
        if ctx.policy.get("must_start_with_if") and not re.match(r"\s*if\b", body, re.I):
            self.emit("C1", "withhold", "drop", body.strip()[:60], "a model forecast that is not conditional",
                      "This forecast was written by a model and is not conditional on its cause.")
        lines_out = []
        for line in body.split("\n"):
            if not line.strip():
                lines_out.append(line)
                continue
            new = self.line(line)
            if new is not None:
                lines_out.append(new)
        self._cur_sentence = None
        result = "\n".join(lines_out).strip("\n")
        self.disclosures(result)
        return result

    def line(self, line):
        self._cur_sentence = line
        if re.search(r"\bUNVERIFIED\s*:", line):
            # Our own marker, written by the model: it would be read as ours.
            self.emit("I1", "drop", "drop", "UNVERIFIED:", "the model wrote the verification marker itself")
            self.dropped.append(line.strip())
            return None
        prefix, sents = _split_line(line)
        kept = []
        for sent, sep in sents:
            new = self.sentence(sent)
            if new is None:
                if self.unit == "line":
                    self.dropped.append(line.strip())
                    return None
                self.dropped.append(sent.strip())
                continue
            if new.strip():
                kept.append([new, sep])
        if not kept:
            return None
        return prefix + "".join(s + sep for s, sep in kept).rstrip()

    # ── one sentence ──
    def sentence(self, s):
        """The sentence after rewrites, or None when it is dropped."""
        self._cur_sentence = s
        before = len(self.findings)

        def dropped():
            return any(f["severity"] in ("drop", "refuse") for f in self.findings[before:])

        self.hard_checks(s)
        if dropped():
            return None
        s = self.action_claims(s)
        s = self.money_claims(s)
        if dropped():
            return None
        s = self.certainty(s)
        s = self.causes(s)
        if dropped():
            return None
        s = self.predictions(s)
        if dropped():
            return None
        s = self.benchmarks(s)
        if dropped():
            return None
        s = self.figures(s)
        if dropped():
            return None
        self.names(s)
        self.topics(s)
        self.directions(s)
        if dropped():
            return None
        return s

    # ── I1 / T1 / A2 ──
    def hard_checks(self, s):
        ctx = self.ctx
        if self.unattended or self.public:
            why = _g.injection_residue(s)
            if why and not (ctx.surface == "social_post" and "link" in why):
                self.emit("I1", "drop", "drop", why.replace("it contains ", ""), why)
            if self.unattended and not self.public and self.untrusted_sh and _g.echoes(s, self.untrusted_sh):
                # No span: the echoed words are a guest's or the manager's.
                self.emit("I1", "drop", "drop", "", "repeats six or more words of untrusted text")
        for name, pat in self.denied:
            if pat.search(s):
                self.emit("T1", "drop", "refuse", name, "names another Cavnar restaurant")
                break
        if _MEMBER_VALUE_RE.search(s) and _g.figure_claims(s):
            m = _MEMBER_VALUE_RE.search(s)
            self.emit("T1", "drop", "refuse", m.group(0), "a single peer's value can disclose a member")
        m = _DISCIPLINE_RE.search(s)
        if m and not self.public:
            self.emit("A2", "drop", "drop", m.group(0), "discipline or firing of staff is never a recommendation")
        m = _FOOD_SAFETY_RE.search(s)
        if m:
            self.emit("A2", "drop", "drop", m.group(0), "a food-safety shortcut")
        self.staffing_cut(s)
        m = _LEGAL_FLAT_RE.search(s)
        if m and not self.public:
            self.emit("A2", "caveat", "drop", m.group(0), "a flat legal statement",
                      "This isn't legal advice — check the rule in force with counsel.")

    def staffing_cut(self, s):
        pol = self.ctx.policy
        floors = {str(k).strip().lower().rstrip("s"): v for k, v in (pol.get("role_floors") or {}).items()}
        # A role with no floor of its own is held to the restaurant's "never
        # cut below" default (policy cut_floor_default, from
        # schedule_rules.cut_policy), and to CUT_FLOOR_DEFAULT when the
        # caller supplied none: never to one person.
        try:
            default = max(1, int(pol.get("cut_floor_default") or CUT_FLOOR_DEFAULT))
        except (TypeError, ValueError):
            default = CUT_FLOOR_DEFAULT
        for m in _CUT_TO_RE.finditer(s):
            raw = m.group("n").lower()
            n = _NUM_WORDS.get(raw, int(raw) if raw.isdigit() else None)
            role = m.group("role").lower().rstrip("s")
            # "cook" in the text is the owner's "Line Cook" or "Pizza Cook":
            # an exact role first, else the smallest floor of the roles it
            # could mean (the owner set those), else the default.
            hits = [floors[role]] if role in floors else \
                [v for k, v in floors.items() if k.split()[-1:] == [role]]
            try:
                floor = max(1, min(int(v) for v in hits)) if hits else default
            except (TypeError, ValueError):
                floor = default
            if n is not None and n < floor:
                self.emit("A2", "drop", "drop", m.group(0), f"cuts {role} below its floor of {floor}")
        keyholders = {_norm_name(k) for k in (pol.get("keyholders") or [])}
        for m in _SEND_HOME_RE.finditer(s):
            who = (m.group("who") or m.group("who2") or "")
            if re.match(r"the\s+(?:only|last|lone)", who, re.I) or _norm_name(who) in keyholders:
                self.emit("A2", "drop", "drop", m.group(0), "sends home the only keyholder or closer")

    # ── P1 (whole public text) ──
    def public_checks(self, body):
        ctx = self.ctx
        never = ",".join(ctx.never_say)
        if ctx.surface in ("reply_public", "guest_sms"):
            why = _g.check_public_reply(body, never)
        else:
            why = _g.check_marketing_copy(body, never)
        if why:
            self.emit("P1", "refuse", span=why.split("(")[-1].rstrip(")") if "(" in why else why, detail=why)
        src = ctx.offer_source.lower()
        for label, pat, owner_may in _P1:
            for m in pat.finditer(body):
                phrase = m.group(0)
                if owner_may and phrase.lower() in src:
                    continue
                self.emit("P1", "refuse", span=phrase, detail=label)
                break
        if ctx.surface == "reply_public":
            # An absolute about how the restaurant runs, in public, is a
            # claim nobody checked (NS2 H8): held for a person to read.
            m = re.search(r"\b(?:always|never|one-time|one\s+time\s+thing|isolated\s+incident)\b", body, re.I)
            if m:
                self.emit("P1", "refuse", span=m.group(0), detail="an absolute claim in a public reply")
            for phrase in _g.unsupported_commitments(body):
                if phrase.lower() not in src:
                    self.emit("P1", "refuse", span=phrase, detail="a commitment nobody told Cavnar was true")
                    break
            # The public-reply claims auto-approve and bulk approve already
            # hold (NS5 H5, ai_guard.public_reply_claims): an explanation
            # nobody gave, a sourcing claim, a guest's private details. What
            # the owner or the guest said themselves is theirs to repeat.
            for claim in _g.public_reply_claims(body, " ".join([ctx.offer_source] + ctx.untrusted)):
                span = claim.split("(", 1)[-1].strip(" )'\"") if "(" in claim else claim
                self.emit("P1", "refuse", span=span, detail=claim)
                break
            # A staff member named in public (a guest's name is allowed).
            names = [n for n in _g.unsupported_names(body, " ".join(list(ctx.names_allowed) + ctx.untrusted))
                     if _norm_name(n) not in {_norm_name(a) for a in ctx.names_allowed}]
            m = re.search(r"(?i:\b(?:sorry\s+about|blame|thanks\s+to))\s+([A-Z][a-z]{2,})\b", body)
            if m and _norm_name(m.group(1)) not in {_norm_name(a) for a in ctx.names_allowed}:
                names.append(m.group(1))
            if names:
                self.emit("P1", "refuse", span=names[0], detail="names a person in public")

    # ── A1 ──
    def action_claims(self, s):
        if self.public:
            return s
        done = {str(v).lower() for v in (self.ctx.policy.get("actions_done") or ())}
        m = _ACTION_CLAIM_RE.search(_blank_own(s))
        if not m or m.group("verb").split()[0].lower() in done:
            return s
        who = m.group("who")
        new_who = "I've" if who.lower().startswith("i") else "we've"
        verb = m.group("verb").split()[0].lower()
        noun = {"texted": " a text to", "emailed": " an email to", "messaged": " a message to",
                "called": " a call to"}.get(verb, "")
        repl = f"{_cap_like(who, new_who)} queued{noun}"
        out = s[:m.start()] + repl + s[m.end():]
        if "for your OK" not in out:
            out = _end_insert(out.rstrip(), " for your OK")
        self.rewrite("A1", m.group(0), repl + " … for your OK")
        return out

    # ── F4 / F6 / F7 / F3 on money claims, by kind ──
    def _claims(self, s):
        """[(ctype, claim)] for every figure the sentence states (memoised
        per text: several rules read the same sentence)."""
        hit = self._claims_memo.get(s)
        if hit is None:
            hit = self._claims_memo[s] = self._read_claims(s)
        return hit

    def _read_claims(self, s):
        t = _normalise(s)
        out = []
        for c in _g.figure_claims(t):
            if c.get("year"):
                continue
            k = c["kind"]
            # The unit is the figure's OWN token: the words right after it,
            # whitespace only between — never past a comma or another figure
            # ("labor ran 34.8%, 8.8 points over" is a % and then points).
            adj = re.match(r"\s*([A-Za-z%★-]+)(?:\s+([A-Za-z%-]+))?", t[c["end"]:c["end"] + 30])
            nxt = [w.lower() for w in (adj.groups() if adj else ()) if w]
            if k == "star" and "-" in c["raw"]:
                continue          # "1-star reviews" names a category, not a rating
            if k == "bare" and nxt and nxt[0] in ("stars", "star", "★"):
                out.append(("star", c))           # a star delta: "by 0.4 stars"
                continue
            if k == "bare" and _MONEY_VERB_BEFORE_RE.search(t[:c["start"]]) and c["value"] >= 10:
                out.append(("money", c))
                continue
            if k in ("bare", "pct") and nxt and (nxt[0] in _POINT_WORDS or
                                                 (nxt[0] == "percentage" and len(nxt) > 1 and nxt[1] in _POINT_WORDS)):
                out.append(("pts", c))
            elif k == "bare":
                if nxt and nxt[0] in _HOUR_WORDS:
                    out.append(("h", c))
                elif c["decimals"] == 0 and re.match(
                        r"^\s+(?:[A-Za-z][\w-]*\s+){0,2}?(?:" + "|".join(re.escape(n) for n in _COUNT_NOUNS) + r")\b",
                        t[c["end"]:c["end"] + 40], re.I) and not re.search(
                        r"\b(?:top|the\s+last|last|next|past|within|in|over|every|each|first)\s*$",
                        t[max(0, c["start"] - 12):c["start"]], re.I):
                    out.append(("count", c))
            else:
                out.append((k, c))
        for m in _MULT_RE.finditer(t):
            out.append(("x", {"kind": "x", "value": float(m.group(1)), "decimals": len(m.group(1).split(".")[1])
                              if "." in m.group(1) else 0, "mult": 1.0, "raw": m.group(0), "start": m.start(),
                              "end": m.end(), "year": False}))
        return t, out

    def _match(self, t, ctype, c):
        """(facts, how) backing claim c: 'direct' | 'diff' | 'change' |
        'ratio' | None."""
        hedged = bool(_HEDGE_NEAR_FIGURE_RE.search(t[:c["start"]]))
        tol = 0.051 if ctype == "star" else (0.5 + 1e-9 if ctype == "count" else _g.precision_tolerance(c, hedged))
        direct = self.facts.direct(ctype, c["value"], tol)
        if direct:
            return direct, "direct", tol
        if self.hybrid:
            ctxf = self._context_facts(ctype, c["value"], tol)
            if ctxf:
                return ctxf, "direct", tol
        der = self.facts.derived(ctype, c["value"], tol)
        if der:
            fs = []
            for pair, _how in der:
                fs += list(pair)
            return fs, der[0][1], tol
        return [], None, tol

    def _context_pool(self):
        """The prompt's own figures, read once per call the way the old
        presence check read them (ai_guard: fences removed, dates and years
        blanked): money {value: {period}}, pct, bare, % differences, and
        the directions the prompt states."""
        if self._ctx_pool is None:
            self._ctx_pool = _context_pool_for(self.ctx.context_text)
        return self._ctx_pool

    @staticmethod
    def _near(sorted_vals, v, tol) -> list:
        i = bisect.bisect_left(sorted_vals, v - tol)
        out = []
        while i < len(sorted_vals) and sorted_vals[i] <= v + tol:
            out.append(sorted_vals[i])
            i += 1
        return out

    def _context_facts(self, ctype, value, tol):
        """Measured facts (source "context") for a figure only the prompt
        text states, from the unit pools the old check used: money from money
        or a bare numeral; % from a % only (F8 reads a % backed only by a bare
        numeral); points from a %, a bare numeral or a difference of two %;
        a rating, count, hours or ratio from a bare numeral. A money figure
        keeps each period the prompt gave it (None when it gave none, which
        F3 then leaves alone, as the old check did)."""
        pool = self._context_pool()
        v = abs(value)
        memo_key = (ctype, v, tol)
        if memo_key in pool["memo"]:
            return pool["memo"][memo_key]
        hits = []
        kinds_of = {}
        if ctype == "money":
            for k in self._near(pool["money_keys"], v, tol):
                hits += [(k, "$", p) for p in (pool["money"][k] or {None})]
                kinds_of[k] = pool["money_kinds"].get(k) or {"measured"}
            if not hits:
                hits = [(k, "", None) for k in self._near(pool["bare"], v, tol)]
        elif ctype == "pct":
            hits = [(k, "%", None) for k in self._near(pool["pct_pool"], v, tol)]
        elif ctype == "pts":
            hits = [(k, "pts", None) for k in self._near(pool["pct_pool"], v, tol)]
            if not hits:
                # a move between two of the prompt's percentages
                vals = pool["pct_pool"]
                for x in vals:
                    if self._near(vals, x + v, tol):
                        hits = [(v, "pts", None)]
                        break
        else:
            unit = {"star": "★", "count": "count", "h": "h", "x": "x"}.get(ctype, "")
            hits = [(k, unit, None) for k in self._near(pool["bare"], v, tol)]
        out = []
        for k, unit, period in hits[:4]:
            dirs = pool["dirs"].get(("money" if ctype == "money" else "pct", k)) or set()
            for kind in sorted(kinds_of.get(k) or {"measured"}):
                out.append(Fact(key="context", value=k, unit=unit, kind=kind, period=period,
                                direction=next(iter(dirs)) if len(dirs) == 1 else None, source="context"))
        pool["memo"][memo_key] = out
        return out

    def money_claims(self, s):
        if not (self.typed or self.legacy):
            # No facts at all: only the realised-money rule can still be read.
            return self._realised_without_facts(s)
        scan = _blank_own(s)
        t, claims = self._claims(scan)
        money = [c for k, c in claims if k == "money"]
        if not money or self.legacy:
            return self._realised_without_facts(s)
        t = _NEGATED_SAVING_RE.sub(lambda m: " " * len(m.group(0)), t)
        # A saving word inside a hedge ("could be recovered", "can save") is
        # no claim that money was saved; and a verb speaks for the figure in
        # its own clause, never for every $ in the sentence ("Waste was
        # $84.50, and about $640 a month could be recovered").
        t = _HEDGED_SAVE_RE.sub(lambda m: " " * len(m.group(0)), t)
        for c in money:
            a, b = _clause_span(t, c["start"], c["end"])
            realised = _REALISED_RE.search(t, a, b)
            loss = _LOSS_RE.search(t, a, b)
            promise = _PROMISE_RE.search(t, a, b)
            facts, how, _tol = self._match(t, "money", c)
            if not facts:
                continue          # F1 reads it later
            kinds = {f.kind for f in facts} if how == "direct" else {"computed"}
            delivered = how == "direct" and any(is_delivered(f) for f in facts)
            if delivered:
                continue
            # A measured loss may be called one ("You wasted $107 this
            # week"); a saving needs a delivered result, and a promise is
            # never made about a measurement.
            verb_m = realised or promise or (loss if not kinds <= {"measured", "computed"} else None)
            if kinds == {"plan"} and not re.search(r"\b(?:budget\w*|target\w*|goal|plan\w*|forecast\w*)\b", t, re.I):
                self.emit("F4", "withhold", "drop", c["raw"], "a budget or target stated as an actual",
                          "A figure here is a budget or target, not what happened.")
                return s
            if kinds <= {"projection", "forecast"} and not (_PROJ_WORD_RE.search(s) or _g.ESTIMATE_WORDS_RE.search(s)) \
                    and re.search(r"\b(?:hit|came\s+in|totall?ed|reached|landed|did|brought\s+in|made)\b", t, re.I):
                self.emit("F4", "withhold", "drop", c["raw"], "a forecast stated as what happened",
                          "A figure here is a forecast, not what happened.")
                return s
            if kinds - {"measured", "computed"} and _MEASURED_WORD_RE.search(t):
                w = _MEASURED_WORD_RE.search(t).group(0)
                self.emit("F4", "withhold", "drop", w, f"{w!r} on a {'/'.join(sorted(kinds))} figure",
                          "A figure here is an estimate or an opportunity, not a measurement.")
                return s
            if verb_m:
                verb = verb_m.group(0)
                if kinds == {"opportunity"} and len(money) == 1:
                    f = facts[0]
                    per = {"month": " a month", "week": " a week", "year": " a year"}.get(f.period, "")
                    amount = re.sub(r"^\s*(?:about|roughly|around)\s+", "", c["raw"])
                    if not amount.startswith("$"):
                        amount = f"${abs(f.value):,.0f}"
                    tail = "a measured loss" if (loss and not realised and not promise) else "money saved"
                    new = f"About {amount}{per} is available (an opportunity, not {tail})."
                    self.rewrite("F4", s.strip(), new)
                    self.emit("F4", "info", span=verb, detail="opportunity worded as money saved or lost")
                    return new
                if kinds <= {"estimate", "projection", "forecast"} and len(money) == 1:
                    new = self._hedge_estimate(s, c, verb, kinds)
                    if new is not None:
                        return new
                self.emit("F4", "withhold", "drop", verb,
                          f"{verb!r} on a {'/'.join(sorted(kinds))} figure — only a measured result is saved or lost",
                          "A figure here is an estimate, a plan or an opportunity — not money already saved.")
                return s
            if kinds == {"opportunity"} and not _OPP_HEDGE_RE.search(s):
                new = _end_insert(s.rstrip(), " (an opportunity, not money saved)")
                self.rewrite("F4", c["raw"], "(an opportunity, not money saved)")
                return new
        return s

    def _realised_without_facts(self, s):
        """"Saved" / "recovered" beside money with no typed fact to show it
        was a delivered result (legacy prompt text, or no money figure the
        facts could place): NS1 C2 — never shown as fact."""
        if self.has_delivered:
            return s
        scan = _blank_own(s)
        t = _NEGATED_SAVING_RE.sub(lambda m: " " * len(m.group(0)), _normalise(scan))
        t = _HEDGED_SAVE_RE.sub(lambda m: " " * len(m.group(0)), t)
        m = _REALISED_RE.search(t)
        if not m:
            return s
        if any(c["kind"] == "money" for c in _g.figure_claims(t)) or \
                re.search(r"\$|\bdollars\b|\bgrand\b|\bmoney\b", t, re.I):
            self.emit("F4", "withhold", "drop", m.group(0),
                      "money called saved or recovered with no measured result behind it",
                      "A figure here is called saved, but no measured result backs it.")
        return s

    _VERB_SWAP = [
        (r"\bhave\s+saved\b|\bhas\s+saved\b|\bsaved\b", "could save"),
        (r"\bare\s+saving\b|\bis\s+saving\b", "could save"),
        (r"\bsaves\s+you\b|\bsaving\s+you\b|\bsave\s+you\b", "could save you"),
        (r"\bwill\s+save\b|['’]ll\s+save\b|\bis\s+going\s+to\s+save\b", "could save"),
        (r"\brecovered\b|\brecouped\b|\bclawed\s+back\b", "could recover"),
        (r"\byou\s+are\s+losing\b|\byou['’]re\s+losing\b", "you may be losing"),
        (r"\bwasted\b", "may have wasted"),
        (r"\bare\s+wasting\b|\bis\s+wasting\b", "may be wasting"),
        (r"\bis\s+costing\s+you\b|\bcosting\s+you\b", "may be costing you"),
        (r"\bcost\s+you\b", "may have cost you"),
        (r"\blost\b", "may have lost"),
    ]

    def _hedge_estimate(self, s, c, verb, kinds):
        """An estimate or projection worded as money saved or lost, with
        its verb softened, "about" before it and its kind named — or None
        when no verb here can be softened ("a saving against target")."""
        new = s
        for pat, repl in self._VERB_SWAP:
            new2 = re.sub(pat, lambda m: _cap_like(m.group(0), repl), new, count=1, flags=re.I)
            if new2 != new:
                new = new2
                break
        else:
            return None
        raw = c["raw"]
        if raw in new and not _HEDGE_NEAR_FIGURE_RE.search(new[:new.find(raw)]):
            new = new.replace(raw, "about " + raw, 1)
        label = " (a projection)" if kinds & {"projection", "forecast"} else " (an estimate)"
        new = _end_insert(new.rstrip(), label)
        self.rewrite("F4", verb, new.strip())
        return new

    # ── C1 ──
    def certainty(self, s):
        L = self.level
        new = s
        for pat, repl in _BANNED:
            def sub(m, repl=repl):
                r = repl[L] if isinstance(repl, dict) else repl
                return _cap_like(m.group(0), r) if r else r
            nxt = pat.sub(sub, new)
            if nxt != new:
                self.rewrite("C1", pat.search(new).group(0).strip(), "(certainty removed)")
                new = nxt
        cond = _CONDITIONAL_RE.search(_blank_own(new))
        if not cond:
            def will(m):
                modal = _MODAL_FOR[L]
                neg = " not" if m.group("neg") else ""
                lead = " " if m.group("will").startswith(("'", "’")) else ""
                return lead + _cap_like(m.group("will"), modal) + neg + " " + (m.group("adv") or "")
            nxt = _WILL_RE.sub(will, new)
            nxt = _WONT_RE.sub(lambda m: _cap_like(m.group(0), _MODAL_FOR[L] + " not "), nxt)
            if nxt != new:
                self.rewrite("C1", "will", _MODAL_FOR[L])
                new = re.sub(r"(\w)\s{2,}", r"\1 ", nxt)
        if L < 3:
            nxt = _SHOULD_RE.sub(lambda m: _cap_like(m.group(0), _MODAL_FOR[L] + " "), new)
            nxt = _LIKELY_TO_RE.sub(lambda m: _MODAL_FOR[L] + " ", nxt)
            nxt = _LIKELY_BE_RE.sub(lambda m: {2: "may be", 1: "might be"}[L], nxt)
            if nxt != new:
                self.rewrite("C1", "should/likely", _MODAL_FOR[L])
                new = nxt
        nxt = _ALWAYS_RE.sub(lambda m: _cap_like(m.group(1), "often" if m.group(1).lower() == "always" else
                                                 "rarely") + " ", new)
        nxt = _EVERY_TIME_RE.sub("Often when", nxt)
        if nxt != new:
            self.rewrite("C1", "always/never", "often/rarely")
            new = nxt
        if new != s:
            new = _tidy(new)
        return new

    # ── K1 ──
    def _anchor_strength(self, clause):
        best = 0
        for a in self.anchors:
            if _g.carries_anchor(clause, [a["text"]]):
                best = max(best, _STRENGTH.get(a["strength"], 2))
        return best

    def _fact_side(self, text):
        """Whether a side of a causal sentence names a fact: a figure that
        matches one, or one of the facts' own key words."""
        t, claims = self._claims(text)
        for k, c in claims:
            tol = 0.051 if k == "star" else _g.precision_tolerance(c, True)
            if self.facts.direct(k, c["value"], tol) or (self.hybrid and self._context_facts(k, c["value"], tol)):
                return True
        roots = {_root(w) for w in re.findall(r"[A-Za-z][A-Za-z'’]{3,}", text)}
        return bool(roots & self.facts.words)

    def causes(self, s):
        pos, rewrites = 0, 0
        scan = _blank_own(s)
        while rewrites < 12 and pos < len(s):
            h = _CAUSAL_HINT_RE.search(scan, pos)
            if not h:
                break
            # Where a match may start is bounded by the hint; the text after
            # it stays visible to the lookaheads (the longest reads ~60 chars).
            m = _CAUSAL_RE.search(scan, max(pos, h.start() - 45), min(len(scan), h.end() + 90))
            if not m or m.start() > h.end():
                pos = h.end()
                continue
            i = int(m.lastgroup[1:])
            _pat, level, fwd, needs_move, r2, r0 = _CAUSAL[i]
            start, end = m.span()
            pos = end
            if needs_move and not _MOVE_PAST_RE.search(scan):
                continue
            quoted = (scan[start:end] + scan[end:end + 30]).lower().strip()
            if len(quoted) > 12 and any(quoted[:len(quoted) - 5] in " ".join(a["text"].lower().split())
                                        for a in self.anchors):
                continue          # the connective is part of the anchor it quotes
            pre = scan[max(0, start - 40):start]
            if _NEGATED_BEFORE_RE.search(pre):
                continue
            if re.match(r"(?:came|comes)\s", scan[start:end], re.I) and re.search(
                    r"\b(?:most|much|all|half|share|bulk|\d+\s?%)\s+of\b", scan[max(0, start - 60):start], re.I):
                continue          # composition of a measured total, not a cause
            if _HEDGE_BEFORE_RE.search(pre):
                level = min(level, 2)
            if level <= 0:
                continue
            clause = s[end:] if fwd else s[:start]
            other = s[:start] if fwd else s[end:]
            if fwd and not s[:start].strip() and "," in clause:
                # "Since X, Y" / "Because X, Y": the cause is X, the effect Y.
                clause, other = clause.split(",", 1)
            strength = self._anchor_strength(clause)
            if strength >= level:
                continue
            matched, rest = s[start:end], s[end:]
            if strength > 0:
                target = r2 if strength >= 2 else r0
            else:
                target = r0 if (self.typed and self._fact_side(clause) and self._fact_side(other)) else None
            repl = target(matched, rest) if callable(target) else target
            if repl is not None:
                repl = _cap_like(matched, repl) if matched[:1].isupper() else repl
                s = s[:start] + repl + s[end:]
                scan = _blank_own(s)
                self.rewrite("K1", matched, repl)
                rewrites += 1
                pos = start + len(repl)
                continue
            self.emit("K1", "caveat", "drop", matched,
                      "a cause nothing stored supports" if not strength else "a cause stated more strongly than its anchor",
                      "Unsupported cause: nothing measured here shows this caused it.")
        # Only a sentence this rule rewrote is tidied: a quoted guest line
        # that starts lower-case is not the engine's to recapitalise.
        return _tidy(s) if (rewrites and s[:1].islower()) else s

    # ── F1 / F2 / F3 / F5 / F6 / F7 / F8 ──
    def figures(self, s):
        if self.legacy:
            self._legacy_figures(s)
            return s
        if not self.typed:
            return s
        scan = _blank_own(s)
        t, claims = self._claims(scan)
        if not claims:
            return s
        mentions = ([(m.start(), m.group(1).lower()) for m in self.entity_re.finditer(t)]
                    if self.entity_re else [])
        on_pace = _ON_PACE_RE.search(t)
        summing = _SUM_RE.search(t)
        for ctype, c in claims:
            facts, how, tol = self._match(t, ctype, c)
            if ctype == "money":
                period, rate = _period_of(t, c)
            else:
                period, rate = None, False
            if not facts:
                if ctype == "money" and summing:
                    combos = self.facts.sums(c["value"], max(tol, 0.005 * abs(c["value"])))
                    mixed = [cb for cb in combos if len({f.kind for f in cb}) > 1 or len({f.period for f in cb}) > 1]
                    if mixed:
                        self.emit("F5", "drop", "drop", c["raw"], "adds figures of different kinds together")
                        return s
                    if combos:
                        continue
                if ctype == "money" and period and rate:
                    s = self._scaled(s, t, c, period)
                    if any(f["severity"] in ("drop", "refuse") for f in self.findings[-1:]):
                        return s
                    if self._scaled_hit:
                        continue
                other = [f for f in self.facts.all if abs(abs(f.value) - abs(c["value"])) <= tol]
                if other:
                    self.emit("F8", "withhold", "drop", c["raw"], f"backed only by a {other[0].unit or 'bare'} figure",
                              f"A figure here isn't backed by a figure of the same unit: {c['raw']}.")
                else:
                    self.emit("F1", "withhold", "drop", c["raw"], "not in the facts",
                              f"Some figures here aren't in your data: {c['raw']}.")
                continue
            if how == "direct" and mentions:
                before = [n for p, n in mentions if p < c["start"]]
                here = before[-1] if before else mentions[0][1]
                allowed = [f for f in facts if f.entity is None or f.entity.lower() == here]
                if not allowed:
                    self.emit("F2", "withhold", "drop", c["raw"], f"attached to {here}",
                              f"A figure here is attached to the wrong day or item: {c['raw']}.")
                    continue
                facts = allowed
            if ctype == "money" and how == "direct":
                s = self._period_checks(s, t, c, facts, period, rate, on_pace)
            kinds = {f.kind for f in facts}
            if how == "direct" and kinds and kinds <= {"estimate", "projection", "forecast"}:
                own = s               # the engine's own label counts as naming it
                if kinds & {"projection", "forecast"}:
                    # A projection names itself (NS3 R4): "about" is not enough.
                    if not (_PROJ_WORD_RE.search(own) or _g.ESTIMATE_WORDS_RE.search(own)):
                        s = _end_insert(s.rstrip(), " (a projection)")
                        self.rewrite("F6", c["raw"], "(a projection)")
                elif not _EST_HEDGE_RE.search(own):
                    raw = c["raw"]
                    if raw in s:
                        s = s.replace(raw, "about " + raw, 1)
                        self.rewrite("F6", raw, "about " + raw)
                    else:
                        s = _end_insert(s.rstrip(), " (an estimate)")
                        self.rewrite("F6", raw, "(an estimate)")
            if ctype == "money" and summing and how == "direct" and len(self._subjects(t)) >= 2 and \
                    not any(re.search(r"total|combined|dedup|sum", f.key.lower()) for f in facts):
                self.emit("F5", "drop", "drop", c["raw"], "a total that is not the sum of what the sentence names")
                return s
        return s

    def _subjects(self, t):
        """The distinct facts a sentence names by a key word or entity."""
        low = t.lower()
        out = set()
        for f in self.facts.all:
            words = [w for w in re.split(r"[_.\W]+", f.key.lower()) if len(w) >= 5 and w not in
                     ("monthly", "weekly", "annual", "dollars", "total", "count", "logged", "value")]
            if any(re.search(r"\b" + re.escape(w[:-1] if w.endswith("s") else w), low) for w in words[-2:]):
                out.add(f.key.split(".")[-1].split("_")[0])
        return out

    _scaled_hit = False

    def _scaled(self, s, t, c, period):
        """A rate figure that is another period's fact scaled: week → month,
        month → year, night → month. Needs a projection word; a year needs
        ≥ 28 data days; a night is never a month."""
        self._scaled_hit = False
        v = abs(c["value"])
        for f in self.facts.by_unit.get("$", []):
            for factor in _SCALE.get((f.period, period), ()):
                if abs(v - abs(f.value) * factor) <= max(0.01 * v, 0.5):
                    self._scaled_hit = True
                    if f.period in ("night", "day") and period in ("month", "year"):
                        self.emit("F3", "withhold", "drop", c["raw"], "one night multiplied into a month or year",
                                  f"A figure here is one night scaled up: {c['raw']}.")
                        return s
                    return self._projection_label(s, t, c, f, period, scaled=True)
        return s

    def _projection_label(self, s, t, c, f, period, scaled=False):
        if period == "year" and (f.data_days is None or f.data_days < 28):
            self.emit("F7", "withhold", "drop", c["raw"],
                      f"a year from {f.data_days} days of data" if f.data_days is not None
                      else "a year from a window of unknown length",
                      "An annual figure here rests on less than four weeks of data.")
            return s
        if not _PROJ_WORD_RE.search(s):
            new = _end_insert(s.rstrip(), " (a projection)")
            self.rewrite("F7", c["raw"], "(a projection)")
            return new
        return s

    def _period_checks(self, s, t, c, facts, period, rate, on_pace):
        kinds = {f.kind for f in facts}
        if facts and all(f.source == "context" for f in facts):
            # A figure only the prompt text holds keeps the old period rule:
            # a stated period must be one the prompt gave that figure, where
            # it gave one; a period-less prompt figure is left alone.
            periods = {f.period for f in facts}
            if period and None not in periods and not any(_compatible(period, p) for p in periods):
                self.emit("F3", "withhold", "drop", c["raw"] + f" a {period}",
                          f"stated per {period}; the data is per {'/'.join(sorted(periods))}",
                          f"A figure here is stated for a different period than the data: {c['raw']}.")
            return s
        if on_pace and not kinds & {"projection", "forecast"}:
            self.emit("F3", "withhold", "drop", on_pace.group(0), "a pace or run rate from a figure that is not one",
                      f"A figure here is stated as a pace it isn't: {c['raw']}.")
            return s
        if not period:
            return s
        periods = {f.period for f in facts}
        if any(p is not None for p in periods):
            if not any(p is not None and _compatible(period, p) for p in periods):
                self.emit("F3", "withhold", "drop", c["raw"] + f" a {period}",
                          f"stated per {period}; the data is per {'/'.join(sorted(p for p in periods if p))}",
                          f"A figure here is stated for a different period than the data: {c['raw']}.")
                return s
        elif rate:
            self.emit("F3", "withhold", "drop", c["raw"] + f" a {period}", "a period the data never gave it",
                      f"A figure here is stated for a different period than the data: {c['raw']}.")
            return s
        if period == "year" and not kinds <= {"measured"}:
            f = next((x for x in facts if x.period == "year"), facts[0])
            return self._projection_label(s, t, c, f, period)
        return s

    def _legacy_figures(self, s):
        """No typed facts: the long-standing presence check against the
        prompt text (ai_guard.unsupported_figures), its misses sorted into
        F1 / F3 / X1, and F8 for a % only a bare numeral backs."""
        ctx = self.ctx
        if self._legacy_bad is None:
            # Once per call: parsing a 60 KB prompt per sentence is what made
            # the old full pass 33 ms (NS6 §C Performance).
            self._legacy_bad = _g.unsupported_figures(self._body, ctx.context_text,
                                                      check_counts=bool(ctx.policy.get("check_counts")))
            self._legacy_known = _g._figures(ctx.context_text)
        bad = []
        for b in list(self._legacy_bad):
            raw = re.split(r" \(it went| a (?:week|month|year|day|night|shift)$", b)[0]
            if raw and raw in _g.normalise_numbers(s):
                bad.append(b)
                self._legacy_bad.remove(b)
        for b in bad:
            if "(it went" in b:
                self.emit("X1", "caveat", "drop", b.split(" (")[0], b,
                          f"A direction here doesn't match the data: {b.split(' (')[0]}.")
            elif re.search(r" a (?:week|month|year|day|night|shift)$", b):
                self.emit("F3", "withhold", "drop", b, "stated for a different period than the data",
                          f"A figure here is stated for a different period than the data: {b}.")
            else:
                self.emit("F1", "withhold", "drop", b, "not in the input",
                          f"Some figures here aren't in your data: {b}.")
        known = self._legacy_known
        for m in _g._PCT_RE.finditer(_g.normalise_numbers(s)):
            try:
                v = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            tol = max(0.02 * abs(v), 0.05) if abs(v) > 10 else 0.051
            if not any(abs(v - k) <= tol for k in known["pct"]) and any(abs(v - k) <= tol for k in known["bare"]):
                self.emit("F8", "withhold", "drop", m.group(0), "a % backed only by a bare number",
                          f"A percentage here isn't backed by a percentage in your data: {m.group(0)}.")

    # ── B1 ──
    @staticmethod
    def _bench_src(f):
        return f.source if isinstance(f.source, dict) else {"source": f.source}

    @staticmethod
    def _bench_platform(src):
        """The all-types group — recognised by where it came from, never by
        its label (BM3-4: "All restaurants on Cavnar" was not in a label
        list, so "like yours" survived on it)."""
        return (src.get("restaurant_category") == "platform" or src.get("engine_kind") == "platform"
                or src.get("source_kind") == "platform")

    def _bench_group(self, src):
        """A Cavnar peer group (a type cohort or the all-types group) — held
        to its size floor — as opposed to a published figure, which is held
        to a source and a year (BM1-1: registry facts carry a label and no
        n, and were dropped as "a cohort under five")."""
        if src.get("source_kind") in ("cohort", "platform") or src.get("engine_kind") in ("peers", "platform") \
                or self._bench_platform(src):
            return True
        if src.get("source_kind"):
            return False
        return src.get("n") is not None

    @staticmethod
    def _bench_topic(f):
        src = f.source if isinstance(f.source, dict) else {}
        key = f"{f.key} {src.get('metric') or ''}"
        return next((k for k, _s, fr in _BENCH_TOPIC_RE if fr.search(key)), None)

    @staticmethod
    def _sentence_topics(t):
        return {k for k, sr, _f in _BENCH_TOPIC_RE if sr.search(t)}

    def benchmarks(self, s):
        if self._p2_bound:
            return s
        scan = _blank_own(s)
        m = _BENCH_RE.search(scan)
        if not m:
            return s
        t, claims = self._claims(scan)
        benches = [f for f in self.facts.benchmarks if f.value is not None]
        sizes = {float(self._bench_src(f).get("n")) for f in benches
                 if isinstance(self._bench_src(f).get("n"), (int, float))}
        # A figure in a benchmark sentence is either the restaurant's own
        # (a fact that is not a benchmark) or a benchmark's; only the latter
        # has to come from a benchmark fact. A peer group's size ("11 other
        # pizza restaurants") is the group's, not a figure to bind.
        own = [c for _k, c in claims if any(f.kind != "benchmark" and abs(abs(f.value) - abs(c["value"])) <=
                                            _g.precision_tolerance(c, True) for f in self.facts.all)
               or (self.hybrid and self._context_facts(_k, c["value"], _g.precision_tolerance(c, True))
                   and not any(abs(abs(f.value) - abs(c["value"])) <= _g.precision_tolerance(c, True)
                               for f in self.facts.benchmarks if f.value is not None))
               or (_k in ("count", "bare") and float(c["value"]) in sizes)]
        bench_claims = [c for _k, c in claims if c not in own]
        # A figure binds only to a benchmark for the measure the sentence is
        # about: "most sports bars run 28% food cost" is not a labor band's
        # 28.5%.
        topics = self._sentence_topics(t)
        matched = []
        for c in bench_claims:
            tol = _g.precision_tolerance(c, True)
            matched += [f for f in benches if abs(abs(f.value) - abs(c["value"])) <= tol
                        and (not topics or self._bench_topic(f) in topics or self._bench_topic(f) is None)]
        if bench_claims and not matched:
            # A peer or industry figure no benchmark fact holds is not said —
            # on an interactive surface too: "most restaurants run 31%"
            # reached owners with a soft label (BM1-1, BM3-2).
            self.emit("B1", "drop", "drop", m.group(0), "a peer or industry figure no benchmark fact holds")
            return s
        if matched:
            pool = matched
        else:
            # A claim with no figure binds to a benchmark for the metric the
            # sentence is about (BM3-5), never to whichever fact came first.
            pool =[f for f in benches if self._bench_topic(f) in topics]
            # The middle of a band stands for the band.
            pool.sort(key=lambda f: 0 if re.search(r"(?:^|[._])(?:p50|median)$", f.key) else 1)
        # A fact that names where it came from before one that does not.
        pool.sort(key=lambda f: 0 if isinstance(f.source, dict) and f.source else 1)
        if not pool:
            self.emit("B1", "caveat", "drop", m.group(0), "a peer or industry comparison with no benchmark behind it "
                      "for the same measure", "A comparison here has no sourced benchmark behind it.")
            return s
        f = pool[0]
        src = self._bench_src(f)
        platform = self._bench_platform(src)
        group = self._bench_group(src)
        if group:
            n = src.get("n")
            min_n = src.get("min_n") if isinstance(src.get("min_n"), (int, float)) else COHORT_MIN_N
            if not isinstance(n, (int, float)) or isinstance(n, bool) or n < min_n:
                self.emit("B1", "drop", "drop", m.group(0),
                          f"a Cavnar peer group under its minimum of {int(min_n)} other restaurants")
                return s
        elif not (src.get("source") and src.get("year")):
            self.emit("B1", "caveat", "drop", m.group(0), "benchmark without a source and year",
                      "A comparison here has no sourced benchmark behind it.")
            return s
        # "Like yours" / "similar to yours" only beside a comparable
        # like-for-like peer group — never the all-types group or a
        # published figure, which are named for what they are.
        lm = _LIKE_YOURS_RE.search(s)
        like_ok = group and not platform and src.get("comparable", True) is not False
        if lm and not like_ok:
            repl = ("other " + PLATFORM_CITE) if platform else \
                (src.get("cohort_label") or "restaurants in the published figure")
            if not platform and lm.group(0)[:1].isupper():
                repl = repl[:1].upper() + repl[1:]
            else:
                repl = _cap_like(lm.group(0), repl)
            s = s[:lm.start()] + repl + s[lm.end():]
            self.rewrite("B1", lm.group(0), repl)
        # A ranking word needs a comparison strong enough to rank on, and
        # the standing the comparison actually shows (BM3-10, BM4 §4.7).
        rk = _RANK_WORD_RE.search(s)
        if rk:
            word = rk.group(0)
            standing = str(src.get("standing") or "")
            sp = src.get("strength_pct")
            top = re.search(r"\b(?:top|upper|best|leads?)\b", word, re.I)
            bottom = re.search(r"\b(?:bottom|lower)\b", word, re.I)
            contradicts = bool(standing) and standing != "unmeasured" and (
                (top and standing not in ("top quarter", "above the middle")) or
                (bottom and standing not in ("bottom quarter", "below the middle")) or
                (re.search(r"quart", word, re.I) and ((top and standing != "top quarter") or
                                                     (bottom and standing != "bottom quarter"))))
            if contradicts:
                self.emit("B1", "caveat", "drop", word, f"a ranking the comparison does not show ({standing})",
                          "A ranking here isn't what the comparison shows.")
                return s
            if not group or not isinstance(sp, (int, float)) or sp < STRONG_CLAIM_PCT:
                if group and standing == "about the middle":
                    repl = "about in line with" if re.match(r"(?:well|far|way)\b", word, re.I) else "about the middle"
                    s = s[:rk.start()] + repl + s[rk.end():]
                    self.rewrite("B1", word, repl)
                else:
                    why = (f"{int(sp)}% comparison strength" if isinstance(sp, (int, float)) else
                           "no comparison strength" if group else "a published figure carries no ranking")
                    self.emit("B1", "caveat", "drop", word, f"a ranking word on a comparison too weak to rank on ({why})",
                              "This comparison isn't strong enough to rank on.")
                    return s
        # Every bound claim names its group: how many and as of when for a
        # peer group, the source and year for a published figure (BM3-5).
        low = s.lower()
        if group:
            n = int(src["n"])
            as_of = src.get("as_of") or f.as_of
            label = PLATFORM_CITE if platform else (src.get("cohort_label") or "restaurants on Cavnar")
            has_n = str(n) in re.findall(r"\d+", s)
            has_date = not as_of or str(as_of) in s
            cite = None
            if not has_n:
                cite = f"{n} other {label}" + (f", as of {as_of}" if as_of else "")
            elif not has_date:
                cite = f"as of {as_of}"
            if cite:
                s = _end_insert(s.rstrip(), f" ({cite})")
                self.rewrite("B1", m.group(0), f"({cite})")
        else:
            named = (str(src["source"]).lower() in low) or (str(src["year"]) in s)
            if not named:
                cite = f"{src.get('source')} {src.get('year')}"
                s = _end_insert(s.rstrip(), f" ({cite})")
                self.rewrite("B1", m.group(0), f"({cite})")
        if src.get("stale"):
            self.emit("B1", "caveat", "drop", m.group(0), "benchmark past its age limit",
                      "A benchmark here is older than its age limit.")
        return s

    # ── P2 ──
    def predictions(self, s):
        """P2: "restaurants like yours reduced X by N%" binds to a
        prediction fact whose value matches N and whose n_restaurants
        clears the floor, or the sentence is dropped. Likelihood words in it
        are capped by the recommendation's own K1 level."""
        self._p2_bound = False
        scan = _blank_own(s)
        m = _PREDICTION_RE.search(scan)
        if not m:
            return s
        raw = m.group("n")
        try:
            v = float(raw)
        except ValueError:
            return s
        d = len(raw.split(".")[1]) if "." in raw else 0
        tol = 0.5 * 10 ** -d + 1e-9
        ok = []
        for f in self.ctx.facts:
            if f.kind != "prediction" or f.value is None or abs(abs(f.value) - v) > tol:
                continue
            src = f.source if isinstance(f.source, dict) else {}
            n = src.get("n_restaurants")
            floor = max(PREDICTION_MIN_RESTAURANTS, src.get("min_n") or 0) \
                if isinstance(src.get("min_n"), (int, float)) else PREDICTION_MIN_RESTAURANTS
            if isinstance(n, (int, float)) and not isinstance(n, bool) and n >= floor:
                ok.append(f)
        if not ok:
            self.emit("P2", "drop", "drop", m.group(0)[:60], "a prediction about similar restaurants with no "
                      "measured prediction behind it")
            return s
        self._p2_bound = True
        L = self.level
        new = s
        for pat, repl in _LIKELIHOOD_RE:
            if repl.get(L) is None:
                continue
            nxt = pat.sub(lambda mm, r=repl: _cap_like(mm.group(0), r[L]), new)
            if nxt != new:
                self.rewrite("P2", pat.search(new).group(0), repl[L])
                new = nxt
        return new

    # ── N1 ──
    def names(self, s):
        ctx = self.ctx
        if self.public or not (ctx.context_text or ctx.names_allowed or ctx.facts):
            return
        if self._name_known is None:
            self._name_known = _g.name_context_words(self.name_ctx)
        known = self._name_known
        # Only a capitalised word the input never held can be a name it
        # never held — most sentences have none, and skip the patterns.
        if not any(w.lower() not in known and w.lower() not in _g._NAME_SKIP
                   for w in re.findall(r"\b[A-Z][a-z]{2,}\b", s)):
            return
        allowed = {_norm_name(n) for n in ctx.names_allowed}
        allowed |= {_root(w) for n in ctx.names_allowed for w in re.findall(r"[A-Za-z'’]+", n)}
        denied = {_norm_name(n) for n, _p in self.denied}
        bad = [n for n in _g.unsupported_names(s, self.name_ctx, known=known)
               if _norm_name(n) not in allowed and _root(n) not in allowed and n.lower() not in _PLATFORM_NAMES
               and _norm_name(n) not in denied and not any(_norm_name(n) in d.split() for d in denied)]
        if bad:
            if ctx.surface in DIAGNOSIS_SURFACES or ctx.policy.get("refuse_on_names"):
                self.emit("N1", "refuse", "refuse", bad[0], "a name the input never held")
            else:
                self.emit("N1", "caveat", "drop", bad[0], "a name the input never held",
                          f"A name here isn't in the data: {bad[0]}.")

    # ── M2 ──
    def topics(self, s):
        missing = [str(x).lower() for x in (self.ctx.data_state.get("missing_inputs") or [])]
        for topic in missing:
            pat = _TOPIC_RE.get(topic)
            if not pat:
                continue
            m = pat.search(s)
            if not m:
                continue
            if topic in ("guests", "sales") and not _OUTCOME_MOVE_RE.search(s):
                continue
            self.emit("M2", "caveat", "drop", m.group(0), f"talks about {topic}, which was not an input",
                      f"This mentions {topic}, which wasn't in the data.")
            return

    # ── X1 ──
    def directions(self, s):
        if not self.typed:
            return
        t, claims = self._claims(_blank_own(s))
        for ctype, c in claims:
            said = _g.claimed_direction(t, c["start"], c["end"])
            if not said:
                continue
            facts, how, _tol = self._match(t, ctype, c)
            if how != "direct":
                continue
            dirs = set()
            for f in facts:
                if f.direction:
                    dirs.add(f.direction)
                elif f.value < 0 and re.search(r"(?:^|[._])(?:vs|delta|change|diff)", f.key.lower()):
                    dirs.add(-1)
                elif f.value > 0 and re.search(r"(?:^|[._])(?:vs|delta|change|diff)", f.key.lower()):
                    dirs.add(1)
            if dirs and said not in dirs:
                went = "up" if 1 in dirs else "down"
                self.emit("X1", "caveat", "drop", c["raw"], f"the data says it went {went}",
                          f"A direction here doesn't match the data: {c['raw']} went {went}.")
                return
        # A movement word about a fact's subject with no figure: "Labor fell"
        # when labor rose.
        for f, subject, pat in self.moves:
            m = pat.search(t)
            if not m:
                continue
            words = set(m.group(1).lower().split())
            said = (-1 if words & (_g.DIRECTION_BEFORE_DOWN | {"down", "slowed"}) else
                    1 if words & (_g.DIRECTION_BEFORE_UP | {"up", "climbed", "jumped", "spiked", "grew"}) else 0)
            if said and said != f.direction:
                self.emit("X1", "caveat", "drop", m.group(0), f"the data says {subject} went "
                          f"{'up' if f.direction > 0 else 'down'}",
                          f"A direction here doesn't match the data: {subject}.")
                return

    # ── M1 (whole text) ──
    def disclosures(self, text):
        ds = self.ctx.data_state
        required = [str(c) for c in (ds.get("required_disclosures") or [])]
        required += [str(c) for c in (ds.get("partial_flags") or []) if str(c) in DISCLOSURES]
        if ds.get("stale_sources"):
            required.append("stale_source")
        for code in dict.fromkeys(required):
            if code not in DISCLOSURES:
                self.findings.append({"rule": "M1", "severity": "info", "span": code[:60],
                                      "detail": "unknown disclosure code", "action": "log"})
                continue
            if _DISCLOSURE_RE[code].search(text or ""):
                continue
            caveat = DISCLOSURES[code][1]
            if code == "stale_source" and ds.get("stale_sources"):
                caveat = f"Some of this data is out of date ({', '.join(str(x) for x in ds['stale_sources'][:4])})."
            self.emit("M1", "caveat", "caveat", code, "a required disclosure the text leaves out", caveat)
        age = ds.get("data_age_days")
        limit = self.ctx.policy.get("max_data_age_days", 7)
        # Present tense only while the registry calls the data current
        # (data_health.validation_state's `not_current`, DH5-3): the card
        # beside the text reads "out of date" from the same registry.
        not_current = [str(x) for x in (ds.get("not_current") or []) if x]
        if (isinstance(age, (int, float)) and age > limit) or not_current:
            m = re.search(r"\b(?:this\s+week|today|tonight|right\s+now|currently|this\s+morning)\b", text or "", re.I)
            if m:
                through = ds.get("as_of")
                why = (f"data is {int(age)} days old" if isinstance(age, (int, float)) and age > limit
                       else f"not current: {', '.join(not_current[:3])}")
                self.emit("M1", "caveat", "caveat", m.group(0), why,
                          f"This reads data through {through}, not this week." if through else
                          (f"This reads data that is {int(age)} days old, not this week."
                           if isinstance(age, (int, float)) else
                           f"This reads data that isn't current ({', '.join(not_current[:3])}), not this week."))


# ── entry points ────────────────────────────────────────────────────────────

def _verdict(run: _Run, text: str) -> Verdict:
    sevs = {f["severity"] for f in run.findings}
    refuse = "refuse" in sevs or (not text.strip() and bool(run.findings) and
                                  any(f["severity"] in ("drop", "refuse") for f in run.findings))
    if refuse:
        v, text = "refuse", ""
        run.controls = False
    elif "withhold" in sevs:
        v = "withhold"
    elif run.caveats or "caveat" in sevs:
        v = "caveat"
    else:
        v = "pass"
    return Verdict(text=text, verdict=v, findings=run.findings,
                   actions={"controls": run.controls and v in ("pass", "caveat"), "caveats": list(run.caveats),
                            "dropped": list(run.dropped), "rewrites": list(run.rewrites)})


def validate(text: str, ctx: ValidationContext) -> Verdict:
    """Check one model output against its context and return the verdict.
    Never raises on a well-formed context; never calls a model."""
    if not isinstance(ctx, ValidationContext):
        raise TypeError("ctx must be a ValidationContext")
    run = _Run(ctx)
    out = run.run(text)
    return _verdict(run, out)


def validate_lines(lines, ctx: ValidationContext) -> LinesResult:
    """Per-line validation for a digest, the DSR or a weekly plan: each line
    is validated alone, and a line with anything dropped is dropped whole."""
    policy = dict(ctx.policy)
    policy["unit"] = "line"
    kept, verdicts, dropped = [], [], []
    for line in lines or []:
        sub = ValidationContext(**{**{k: getattr(ctx, k) for k in ctx.__dataclass_fields__}, "policy": policy})
        v = validate(str(line or ""), sub)
        verdicts.append(v)
        if v.verdict == "refuse" or not v.text.strip():
            dropped.append(str(line or ""))
        else:
            kept.append(v.text)
    return LinesResult(lines=kept, verdicts=verdicts, dropped=dropped)


# ── rollout mode ────────────────────────────────────────────────────────────
#
# RESPONSE_VALIDATION_MODE is JSON {surface: "shadow"|"enforce", "*": …}.
# Default: enforce everywhere. Unattended and public surfaces are where an
# unverified sentence does the most harm, and the interactive surfaces'
# rewrites only ever lower a claim; the golden corpus is the gate. A
# surface set to "shadow" computes and logs the verdict but shows the
# original text (apply()).

def mode_for(surface: str) -> str:
    raw = os.environ.get("RESPONSE_VALIDATION_MODE") or ""
    try:
        conf = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        conf = {}
    if not isinstance(conf, dict):
        conf = {}
    mode = conf.get(surface) or conf.get("*") or "enforce"
    return mode if mode in ("shadow", "enforce") else "enforce"


def apply(text: str, ctx: ValidationContext, log_it: bool = True) -> tuple:
    """(text to show, verdict) under the surface's mode: enforce shows the
    verdict's text; shadow shows the original and only logs."""
    v = validate(text, ctx)
    mode = mode_for(ctx.surface)
    if log_it:
        log(v, ctx, mode=mode, original=text)
    return (v.text if mode == "enforce" else str(text or "")), v


def text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def log(verdict: Verdict, ctx: ValidationContext, mode: str = None, original: str = None, db_path=None) -> bool:
    """Record a verdict in ai_validation_log (ai_utils.log_validation). No
    answer or guest text is stored: rule codes, counts, a hash of the
    original, and each finding's offending token cut to 60 characters (an
    echo finding stores none). Never raises; False when not recorded."""
    try:
        import ai_utils
        ai_utils.log_validation(
            restaurant_id=ctx.restaurant_id, surface=ctx.surface,
            action=ctx.policy.get("action") or ctx.surface, verdict=verdict.verdict,
            rules=verdict.codes, tokens=[f["span"][:60] for f in verdict.findings if f.get("span")],
            n_rewrites=len(verdict.actions.get("rewrites") or []),
            n_drops=len(verdict.actions.get("dropped") or []),
            n_caveats=len(verdict.actions.get("caveats") or []),
            text_hash=text_hash(original if original is not None else verdict.text),
            mode=mode or mode_for(ctx.surface), version=verdict.version, db_path=db_path)
        return True
    except Exception:
        return False


# ── adoption helpers (workstream A) ─────────────────────────────────────────
#
# Every model call site runs the engine after parsing and before storing,
# caching or sending (PROMPT_LIBRARY.md → Response Validation → Adoption):
#
#     out = response_validation.enforce(text, ctx)   # a str, with .validation
#     store / return out
#
# `enforce` validates under the surface's mode, logs the verdict, and returns
# the text to show as a `Validated` str carrying the structured `validation`
# object ({verdict, caveats, controls, codes, version}) that payloads emit.
# Until web and iOS read that object, the old "UNVERIFIED: …" line is kept
# on the text whenever the verdict has a caveat or withholds controls (the
# clients' promote / controls logic reads it); its words are the caveats.

class Validated(str):
    """The text an owner is shown, as a plain str (it serialises, compares
    and concatenates as one), carrying the verdict it came from:
    `.validation` (the payload dict) and `.verdict` (the Verdict, or None
    for a text that was not validated in this process)."""
    validation = None
    verdict = None

    def __new__(cls, text="", validation=None, verdict=None):
        obj = super().__new__(cls, text or "")
        obj.validation = validation
        obj.verdict = verdict
        return obj


def payload(verdict: Verdict) -> dict:
    """The structured `validation` object an API payload carries beside the
    text: {verdict, caveats, controls, codes, version}."""
    if verdict is None:
        return None
    return {"verdict": verdict.verdict, "caveats": list(verdict.actions.get("caveats") or []),
            "controls": bool(verdict.actions.get("controls")), "codes": verdict.codes,
            "version": verdict.version}


def validation_of(text) -> dict:
    """The `validation` object a text carries (a Validated str), or None."""
    return getattr(text, "validation", None)


_MARKER_RE = re.compile(r"(?is)\n*\s*UNVERIFIED:\s*.*$")


def strip_marker(text) -> str:
    """The text without a trailing "UNVERIFIED: …" line (a stored read from
    before the engine, about to be re-validated)."""
    return _MARKER_RE.sub("", str(text or "")).rstrip()


def legacy_note(verdict: Verdict):
    """The words of the legacy "UNVERIFIED:" line — the verdict's caveats —
    or None when the verdict carries none and keeps its controls."""
    if verdict is None or verdict.verdict in ("pass", "refuse"):
        return None
    cav = [c.rstrip(".") for c in verdict.actions.get("caveats") or [] if c]
    if not cav:
        cav = [RULES.get(code, code) for code in verdict.codes
               if any(f["rule"] == code and f["severity"] in ("caveat", "withhold") for f in verdict.findings)]
    return ("; ".join(dict.fromkeys(cav)) + ".") if cav else None


def enforce(text: str, ctx: ValidationContext, *, marker: bool = True, log_it: bool = True) -> Validated:
    """Validate `text` under its surface's mode, log the verdict, and return
    the text to show (a Validated str with `.validation`). Refused → "" (the
    caller shows its fixed copy or the previous read). With `marker`, a
    verdict that carries caveats or withholds controls keeps the legacy
    "UNVERIFIED: …" line (its caveats) until the clients read the object."""
    shown, v = apply(text, ctx, log_it=log_it)
    out = shown
    if marker and shown and mode_for(ctx.surface) == "enforce":
        note = legacy_note(v)
        if note:
            out = shown.rstrip() + "\n\nUNVERIFIED: " + note
    return Validated(out, validation=payload(v), verdict=v)


def entity_facts(entities, globals_=(), *, kind_map=None, data_days=None) -> list:
    """Typed facts from a module's existing binding builder
    ({entity: [values]}, [global values]) — labor_insight_facts,
    food_insight_facts: a number is a fact of that entity in each unit it
    could be written in ($, %, bare; the old binding check was unit-blind);
    a dict is typed by its keys (facts_from_dict); a string's own figures
    (a driver's evidence line) are facts of their written unit. Globals get
    no entity (any sentence may quote them). Kinds default to measured —
    money whose kind matters is passed as its own typed Fact beside these."""
    out = []

    def add(v, entity, prefix):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            for u in ("$", "%", ""):
                out.append(Fact(key=prefix, value=v, unit=u, kind="measured", entity=entity, data_days=data_days))
        elif isinstance(v, dict):
            out.extend(facts_from_dict(v, kind_map, entity=entity, prefix=prefix, data_days=data_days))
        elif isinstance(v, (list, tuple)):
            for x in v:
                add(x, entity, prefix)
        elif isinstance(v, str):
            for c in _g.figure_claims(_g.normalise_numbers(v)):
                if c.get("year"):
                    continue
                u = {"money": "$", "pct": "%", "star": "★"}.get(c["kind"], "")
                out.append(Fact(key=prefix, value=c["value"], unit=u, kind="measured", entity=entity,
                                data_days=data_days))

    for name, vals in (entities or {}).items():
        if name in (None, ""):
            continue
        add(list(vals) if isinstance(vals, (list, tuple, set)) else vals, str(name),
            "entity." + re.sub(r"\W+", "_", str(name).lower()).strip("_"))
    for v in globals_ or ():
        add(v, None, "global")
    return out


def anchor(text, strength="likely") -> list:
    """[{text, strength}] for one cause anchor, or [] when there is none.
    The strengths every call site uses: a diagnosis's cause and a ranked
    driver are "likely"; co-movement / operational evidence and an
    alternative cause are "association"; a recommended action is never an
    anchor."""
    t = " ".join(str(text or "").split())
    return [{"text": t, "strength": strength}] if t else []
