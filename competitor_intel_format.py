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


def parse_competitor_intel(text):
    """Shared parser behind format_intel/format_intel_body — these two used
    to each independently re-implement the same normalize/split/classify
    pass. Returns {"intro": str, "sections": [(name, [bullet,...])],
    "recommendations": [str, ...], "normalized_text": str} — callers' no-
    structure-found fallback renders normalized_text (not the raw input),
    matching the original functions' in-place `text = re.sub(...)` behavior."""
    normalized_text = normalize_intel_text(text)
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
        elif re.match(r"^[0-9]+[.)]\s+", line):
            rec_lines.append(re.sub(r'\*+', '', re.sub(r"^[0-9]+[.)]\s+", "", line)).strip())
        elif current_section == "recommendations" and line and not re.search(r"Recommendations?", line, re.I):
            cleaned = re.sub(r'\*+', '', line).strip()
            if cleaned:
                rec_lines.append(cleaned)
    flush()

    return {"intro": " ".join(intro_lines), "sections": sections, "recommendations": rec_lines,
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
    html_parts = [p for p in html_parts if p]
    if not html_parts:
        return Markup('<p style="font-size:13px;color:var(--ink);line-height:1.7">' + str(esc(parsed["normalized_text"])) + "</p>")
    return Markup("".join(html_parts))


def extract_recs(text):
    """Parse recommendation lines from competitor insight. Returns list of strings.

    Deliberately NOT routed through parse_competitor_intel(): that parser's
    recommendation-collection (shared by format_intel/format_intel_body)
    treats *any* numbered line anywhere as a recommendation, even one that
    shows up inside WELL/POORLY before a "Recommendations:" header is ever
    seen. This function anchors strictly to the labeled header instead, and
    the two behaviors provably disagree on that edge case — unifying them
    would be a real (if narrow) behavior change, not a pure de-dup."""
    if not text:
        return []
    text = normalize_intel_text(text)
    recs = []
    in_recs = False
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^Recommendations?:\s*$", line, re.I):
            in_recs = True
            continue
        if in_recs:
            # Split any inline numbered items on this line before processing
            parts = re.split(r'(?<=\S)\s+(?=\d+\.\s+[A-Z])', line)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                part = re.sub(r'^[0-9]+[.)]\s+', '', part).strip()
                if part and not re.match(r'^(WHAT COMPETITORS|Recommendations?)', part, re.I):
                    recs.append(part)
    return recs[:3]
