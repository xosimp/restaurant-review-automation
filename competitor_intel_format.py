"""
competitor_intel_format.py — parses the structured text Claude returns for
competitor intel (WHAT COMPETITORS ARE DOING WELL/POORLY + Recommendations)
into the pieces the dashboard renders.

Used to be 3 independent ~80-line copies living inline as Jinja template
filters in hosted_dashboard.py (format_intel, format_intel_body,
extract_recs) — each re-implementing the same normalize/split/classify pass
with small drifts between copies, and untestable without importing
hosted_dashboard.py itself (which runs real DB init and background threads
at module import time). Extracted here as plain functions so the parsing
logic has a home that's actually importable in tests; hosted_dashboard.py
just registers thin Jinja-filter wrappers around these.
"""
import re

import thresholds as _thr


def normalize_intel_text(text):
    """Strip markdown, em-dashes to hyphens, ensure section headers are on
    their own line — shared by every parser below."""
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'[–—]', '-', text)
    text = re.sub(r'(?i)(WHAT COMPETITORS ARE DOING WELL):', '\nWHAT COMPETITORS ARE DOING WELL:\n', text)
    text = re.sub(r'(?i)(WHAT COMPETITORS ARE DOING POORLY):', '\nWHAT COMPETITORS ARE DOING POORLY:\n', text)
    text = re.sub(r'(?i)(PRICE POSITIONING):', '\nPRICE POSITIONING:\n', text)
    text = re.sub(r'(?i)Recommendations?:', '\nRecommendations:\n', text)
    return text


# A recommendation cites the competitor reviews it rests on as "[R2, R5]"
# at the end of the line (competitor.generate_competitor_insight numbers
# every review it hands the model and drops any recommendation whose
# citations do not resolve).
_CITE_RE = re.compile(r"\s*\[((?:R\d+\s*,?\s*)+)\]\s*\.?\s*$", re.I)
# The model's honest "nothing to do" answer under Recommendations.
NOTHING_TO_ACT_ON = "Nothing worth acting on this week."
_NOTHING_RE = re.compile(r"^nothing (?:worth acting on|to act on)", re.I)
_UNVERIFIED_RE = re.compile(r"(?is)\n*\s*UNVERIFIED:\s*(.+)$")


def split_citations(line):
    """("line without its citation", ["R2", "R5"])."""
    m = _CITE_RE.search(line or "")
    if not m:
        return (line or "").strip(), []
    cites = [c.strip().upper() for c in re.split(r"[,\s]+", m.group(1)) if c.strip()]
    return (line[:m.start()].rstrip(" .") + ("." if line[:m.start()].rstrip().endswith(".") else "")).strip(), cites


def parse_competitor_intel(text):
    """THE parser for competitor intel — every surface reads this.

    Returns {"intro", "sections": [(name, [bullet,...])], "recommendations":
    [str, ...], "recommendation_items": [{"text", "cites"}], "unverified":
    str|None, "withheld_recommendations": int, "nothing_to_act_on": bool,
    "normalized_text"}.

    Recommendations are anchored strictly to the "Recommendations:" header.
    There used to be two parsers that disagreed — this one took any numbered
    line anywhere, extract_recs only lines under the header — so the Intel
    screen, the Home tile and Ask could show different recommendation counts
    for one insight (audit #31). extract_recs now reads this.

    An insight carrying an UNVERIFIED flag (a figure or a business the model
    stated that its input did not contain) promotes NO recommendations:
    `recommendations` is empty and `withheld_recommendations` says how many
    were held back, so no surface turns an unverified line into an action.
    """
    raw = text or ""
    unverified = None
    um = _UNVERIFIED_RE.search(raw)
    if um:
        unverified = um.group(1).strip().rstrip(".") or None
        raw = raw[:um.start()]
    normalized_text = normalize_intel_text(raw)
    text = normalized_text
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    intro_lines = []
    section_lines = []
    in_section = False
    for line in lines:
        if re.match(r"^(WHAT COMPETITORS|PRICE POSITIONING|Recommendations?:?)", line, re.I):
            in_section = True
        if in_section:
            section_lines.append(line)
        else:
            intro_lines.append(line)

    sections = []
    current_section = None
    bullets = []
    rec_lines = []

    def flush():
        if current_section and current_section != "recommendations" and bullets:
            sections.append((current_section, list(bullets)))

    for line in section_lines:
        if re.match(r"WHAT COMPETITORS ARE DOING WELL", line, re.I):
            flush()
            current_section = "What competitors are doing well"
            bullets = []
        elif re.match(r"WHAT COMPETITORS ARE DOING POORLY", line, re.I):
            flush()
            current_section = "What competitors are doing poorly"
            bullets = []
        elif re.match(r"PRICE POSITIONING", line, re.I):
            # The paid "where these competitors sit on price" line. It is one
            # sentence, not a bullet, and the parser only kept "-" lines, so
            # it was dropped on every surface (M-28).
            flush()
            current_section = "Price positioning"
            bullets = []
        elif re.search(r"Recommendations?", line, re.I) and not line.startswith("-") and not re.match(r"^[0-9]", line):
            flush()
            current_section = "recommendations"
            bullets = []
        elif line.startswith("-") and current_section != "recommendations":
            b = re.sub(r'\*+', '', line.lstrip("- ")).strip()
            if b:
                bullets.append(b)
        elif current_section == "recommendations" and line and not re.match(r"^Recommendations?:?\s*$", line, re.I):
            # Inline numbered items on one line are split, as before.
            for part in re.split(r'(?<=\S)\s+(?=\d+\.\s+[A-Z])', line):
                part = re.sub(r'\*+', '', re.sub(r"^[0-9]+[.)]\s+", "", part.strip())).strip()
                if part and not re.match(r'^(WHAT COMPETITORS|Recommendations?)', part, re.I):
                    rec_lines.append(part)
        elif current_section == "Price positioning" and line:
            b = re.sub(r'\*+', '', line).strip()
            if b:
                bullets.append(b)
    flush()

    nothing = any(_NOTHING_RE.match(r) for r in rec_lines)
    items = []
    for r in rec_lines:
        if _NOTHING_RE.match(r):
            continue
        body, cites = split_citations(r)
        if body:
            items.append({"text": body, "cites": cites})
    items = items[:3]
    withheld = 0
    if unverified and items:
        withheld, items = len(items), []
    return {"intro": " ".join(intro_lines), "sections": sections,
            "recommendations": [it["text"] for it in items],
            "recommendation_items": items,
            "unverified": unverified,
            "withheld_recommendations": withheld,
            "nothing_to_act_on": bool(nothing and not items),
            "normalized_text": normalized_text}


def render_intro(intro, esc):
    if not intro:
        return ""
    return '<p style="font-size:13px;color:var(--ink);line-height:1.7;margin-bottom:14px">' + str(esc(intro)) + "</p>"


def section_tone(name):
    """'good' (doing well), 'bad' (doing poorly) or 'neutral' (price
    positioning — a fact about the market, not a verdict)."""
    up = (name or "").upper()
    return "good" if "WELL" in up else ("bad" if "POORLY" in up else "neutral")


def render_section(name, bullets, esc):
    if not bullets:
        return ""
    tone = section_tone(name)
    color = {"good": "var(--green)", "bad": "var(--red)"}.get(tone, "var(--ink3)")
    icon = {"good": "✓", "bad": "✗"}.get(tone, "·")
    out = '<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:' + color + ';margin:14px 0 8px">' + name + "</div>"
    for b in bullets:
        out += (
            '<div style="display:flex;gap:8px;margin-bottom:6px;align-items:flex-start">'
            + '<span style="flex-shrink:0;color:' + color + ';font-weight:700;font-size:13px">' + icon + "</span>"
            + '<span style="font-size:13px;color:var(--ink);line-height:1.6">' + str(esc(b)) + "</span></div>"
        )
    return out


def render_unverified(note, withheld, esc):
    """The caveat under an insight the checks could not confirm — never
    styled like a recommendation, and saying that any were held back."""
    if not note:
        return ""
    held = (f" {withheld} recommendation{'' if withheld == 1 else 's'} held back until it is." if withheld else "")
    return ('<div style="margin-top:10px;padding:10px 12px;border-left:2px solid var(--amber);'
            'border-radius:0 6px 6px 0"><div style="font-size:10px;font-weight:700;text-transform:uppercase;'
            'letter-spacing:.08em;color:var(--amber);margin-bottom:4px">Unverified</div>'
            '<div style="line-height:1.6;color:var(--ink2);font-size:13px">This read '
            + str(esc(note)) + "." + held + "</div></div>")


def render_recommendations(rec_lines, esc):
    if not rec_lines:
        return ""
    out = ['<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#c84b2f;margin:14px 0 8px">Recommendations</div>']
    for i, rec in enumerate(rec_lines, 1):
        out.append(
            '<div style="display:flex;gap:10px;margin-bottom:8px;align-items:flex-start">'
            + '<span style="flex-shrink:0;width:20px;height:20px;border-radius:50%;background:#c84b2f;color:white;font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center">' + str(i) + "</span>"
            + '<span style="line-height:1.6;color:#b7791f;font-weight:500">' + str(esc(rec)) + "</span></div>"
        )
    return "".join(out)


def format_intel(text):
    """Parse structured competitor intel into formatted HTML matching labor/inventory style."""
    from markupsafe import Markup, escape as esc
    if not text:
        return '<p style="color:var(--ink3);font-size:13px">Analysis unavailable.</p>'
    parsed = parse_competitor_intel(text)
    html_parts = [render_intro(parsed["intro"], esc)]
    html_parts += [render_section(name, bullets, esc) for name, bullets in parsed["sections"]]
    html_parts.append(render_recommendations(parsed["recommendations"], esc))
    html_parts.append(render_unverified(parsed["unverified"], parsed["withheld_recommendations"], esc))
    html_parts = [p for p in html_parts if p]
    if not html_parts:
        return '<p style="font-size:13px;color:#374151;line-height:1.7">' + str(esc(parsed["normalized_text"])) + "</p>"
    return Markup("".join(html_parts))


def format_intel_body(text):
    """Same as format_intel but omits recommendations — only intro + well/poorly."""
    from markupsafe import Markup, escape as esc
    if not text:
        return Markup('')
    parsed = parse_competitor_intel(text)
    html_parts = [render_intro(parsed["intro"], esc)]
    html_parts += [render_section(name, bullets, esc) for name, bullets in parsed["sections"]]
    html_parts.append(render_unverified(parsed["unverified"], parsed["withheld_recommendations"], esc))
    html_parts = [p for p in html_parts if p]
    if not html_parts:
        return Markup('<p style="font-size:13px;color:var(--ink);line-height:1.7">' + str(esc(parsed["normalized_text"])) + "</p>")
    return Markup("".join(html_parts))


def extract_recs(text):
    """The recommendation lines of a competitor insight, as plain strings
    (at most three, citations removed). A thin reader over
    parse_competitor_intel — it used to be a second parser that disagreed
    with the first (audit #31); now every surface counts the same lines, and
    an insight carrying an UNVERIFIED flag yields none."""
    if not text:
        return []
    return parse_competitor_intel(text)["recommendations"][:3]


# ── The market average: one definition (CA1 I11, fix I10) ─────────────────
#
# There were three: the web Intel header took a flat mean of competitor
# ratings and compared it with the average of the reviews Cavnar imported;
# the phone took a review-weighted mean against Google's all-time rating;
# the welcome email (first_look) took a flat mean of whatever Places
# returned. The same restaurant could lead its block on one surface and
# trail it on another. This is the phone's definition, for every surface.
#
# Local market standing (Benchmarking #38; BM1-13, BM3-18). "Behind the
# block" rested on one bar 8 km away: the standing had no minimum, admitted
# the rivals competitor.py's widened-radius fallback picked up (no cuisine
# and no price match), let one high-volume venue dominate the average, and
# painted a tie amber. Now:
#   * a standing needs MARKET_MIN_MATCHED rated rivals matched on cuisine
#     AND price (or added by the owner, who chose them as the competition);
#   * a rival from the widened search never enters the average;
#   * one venue's weight is capped at MARKET_WEIGHT_CAP_REVIEWS reviews —
#     past that its rating's standard error is negligible, so more reviews
#     buy it no more say;
#   * n and the radius travel with the standing (`standing_basis`);
#   * "level" is a symmetric, neutral band inside the standard error of the
#     gap (thresholds.RATING_SIGMA over both sides' review counts, never
#     under MARKET_TIE_MIN_STARS), so a tie is neither a win nor amber.

MARKET_MIN_MATCHED = 3
MARKET_WEIGHT_CAP_REVIEWS = 500
# The published rating step: a gap of one step is always a tie.
MARKET_TIE_MIN_STARS = 0.1
# The lead a standing needed before the tie band was measured; still the
# band when the owner's own review count is unknown (the conservative bar).
MARKET_LEAD_STARS = 0.3
# The old lower edge of the (asymmetric, amber) "neck and neck" band. No
# longer read: the tie band is symmetric and measured (market_standing).
# Candidate for future cleanup after additional verification.
MARKET_LEVEL_STARS = -0.1

# competitor.py's match_basis strings, by selection pass.
_MATCHED_BASES = ("same cuisine type and similar price level", "added by you")
_WIDENED_BASES = ("widened search",)


def market_member(c) -> str:
    """How a competitor entered the set: "matched" (same cuisine and similar
    price, or added by the owner), "cuisine_only" (price not matched),
    "widened" (the doubled-radius fallback: no cuisine, no price) or
    "unknown" (a read stored before selection provenance existed)."""
    basis = str((c or {}).get("match_basis") or "").strip().lower()
    if not basis:
        return "unknown"
    if basis.startswith(_WIDENED_BASES):
        return "widened"
    if basis.startswith(_MATCHED_BASES):
        return "matched"
    return "cuisine_only"


def market_rating(competitors) -> dict:
    """The competitor set's rating, weighted by review volume (each venue
    capped at MARKET_WEIGHT_CAP_REVIEWS), leaving out provisional ratings
    (a four-review venue at 5.0 would otherwise pull the market the owner
    is measured against) and every rival the widened-radius fallback picked
    up. Unrated places are absent, never a zero.
    {"market_rating", "market_rating_reviews", "market_rating_n",
     "market_matched_n", "market_excluded_widened", "market_radius_km",
     "market_effective_reviews"}."""
    rated = []
    widened = 0
    for c in competitors or []:
        try:
            r = float(c.get("rating") or 0)
            n = int(c.get("review_count") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if not r or c.get("rating_is_provisional"):
            continue
        member = market_member(c)
        if member == "widened":
            widened += 1
            continue
        dist = c.get("distance_m")
        try:
            dist = float(dist) if dist is not None else None
        except (TypeError, ValueError):
            dist = None
        rated.append((r, n, member, dist))
    if not rated:
        return {"market_rating": None, "market_rating_reviews": 0, "market_rating_n": 0,
                "market_matched_n": 0, "market_excluded_widened": widened, "market_radius_km": None,
                "market_effective_reviews": 0}
    weights = [min(max(n, 0), MARKET_WEIGHT_CAP_REVIEWS) for _r, n, _m, _d in rated]
    total_w = sum(weights)
    weighted = (sum(r * w for (r, _n, _m, _d), w in zip(rated, weights)) / total_w) if total_w > 0 else \
        sum(r for r, _n, _m, _d in rated) / len(rated)
    dists = [d for _r, _n, _m, d in rated if d is not None]
    return {"market_rating": round(weighted, 1), "market_rating_reviews": sum(n for _r, n, _m, _d in rated),
            "market_rating_n": len(rated),
            "market_matched_n": sum(1 for _r, _n, m, _d in rated if m == "matched"),
            "market_excluded_widened": widened,
            "market_radius_km": round(max(dists) / 1000.0, 1) if dists else None,
            "market_effective_reviews": total_w}


def own_rating(gbp_rating=None, gbp_review_count=None, sample_rating=None, sample_count=None) -> dict:
    """The owner's rating, from one source: Google's all-time rating when
    Cavnar has it (the only figure comparable to competitors' all-time
    ratings), else the imported sample, labelled as such.
    {"own_rating", "own_rating_basis", "own_rating_count"}."""
    if gbp_rating:
        return {"own_rating": round(float(gbp_rating), 1), "own_rating_basis": "google_all_time",
                "own_rating_count": gbp_review_count}
    if sample_rating:
        return {"own_rating": round(float(sample_rating), 2), "own_rating_basis": "imported_sample",
                "own_rating_count": sample_count}
    return {"own_rating": None, "own_rating_basis": None, "own_rating_count": None}


def standing_margin(own_count, market_effective_reviews) -> float:
    """The half-width of the "level" band, in stars: one standard error of
    the gap (thresholds.RATING_SIGMA over the owner's review count and the
    market's effective count), never under MARKET_TIE_MIN_STARS; the old
    MARKET_LEAD_STARS bar when the owner's count is unknown."""
    try:
        n_own = int(own_count or 0)
    except (TypeError, ValueError):
        n_own = 0
    if n_own <= 0:
        return MARKET_LEAD_STARS
    try:
        n_mkt = float(market_effective_reviews or 0)
    except (TypeError, ValueError):
        n_mkt = 0.0
    var = 1.0 / n_own + (1.0 / n_mkt if n_mkt > 0 else 0.0)
    return round(max(MARKET_TIE_MIN_STARS, _thr.RATING_SIGMA * var ** 0.5), 2)


def market_standing(own: dict, market: dict) -> dict:
    """Where the owner stands against the market, from market_rating() and
    own_rating(). Compared only when the own rating is Google's all-time
    one — an imported sample is not the same kind of number as the
    competitors' ratings — and only on MARKET_MIN_MATCHED matched rivals.
    {"own_vs_market", "standing": "ahead" | "level" | "behind" | None,
     "standing_label", "standing_tone", "standing_basis", "standing_margin",
     "standing_why_not"}."""
    own, market = own or {}, market or {}
    o, m = own.get("own_rating"), market.get("market_rating")
    none = {"own_vs_market": None, "standing": None, "standing_label": None, "standing_tone": "neutral",
            "standing_basis": None, "standing_margin": None, "standing_why_not": None}
    if o is None or m is None or own.get("own_rating_basis") != "google_all_time":
        return none
    matched = int(market.get("market_matched_n") or 0)
    if matched < MARKET_MIN_MATCHED:
        return dict(none, standing_why_not=(
            f"needs {MARKET_MIN_MATCHED} nearby restaurants matched on cuisine and price with a settled "
            f"rating (has {matched})"))
    gap = round(float(o) - float(m), 1)
    margin = standing_margin(own.get("own_rating_count"), market.get("market_effective_reviews"))
    if gap > margin:
        standing, label, tone = "ahead", "You lead the block", "good"
    elif gap < -margin:
        standing, label, tone = "behind", "Behind the block", "bad"
    else:
        standing, label, tone = "level", "About level with the block", "neutral"
    radius = market.get("market_radius_km")
    basis = (f"{matched} restaurant{'' if matched == 1 else 's'} matched on cuisine and price"
             + (f" within {radius:g} km" if radius else "")
             + f" · a gap inside ±{margin:.1f}★ reads as level")
    return {"own_vs_market": gap, "standing": standing, "standing_label": label, "standing_tone": tone,
            "standing_basis": basis, "standing_margin": margin, "standing_why_not": None}
