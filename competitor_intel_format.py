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


def normalize_intel_text(text):
    """Strip markdown, em-dashes to hyphens, ensure section headers are on
    their own line — shared by every parser below."""
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'[–—]', '-', text)
    text = re.sub(r'(?i)(WHAT COMPETITORS ARE DOING WELL):', '\nWHAT COMPETITORS ARE DOING WELL:\n', text)
    text = re.sub(r'(?i)(WHAT COMPETITORS ARE DOING POORLY):', '\nWHAT COMPETITORS ARE DOING POORLY:\n', text)
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
        if re.match(r"^(WHAT COMPETITORS|Recommendations?:?)", line, re.I):
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


def render_section(name, bullets, esc):
    if not bullets:
        return ""
    is_good = "WELL" in name.upper()
    color = "#16a34a" if is_good else "#dc2626"
    icon = "✓" if is_good else "✗"
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
