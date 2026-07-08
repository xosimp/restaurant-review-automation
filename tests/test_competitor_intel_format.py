"""competitor_intel_format.py — extracted from 3 independent ~80-line inline
Jinja filters in hosted_dashboard.py (format_intel, format_intel_body,
extract_recs) that each re-implemented the same normalize/split/classify
pass with small drifts between copies, and were untestable in that form
since importing hosted_dashboard.py runs real DB init and background
threads at module import time."""
from competitor_intel_format import format_intel, format_intel_body, extract_recs

SAMPLE = """This area has 4 nearby competitors with strong Italian/pizza offerings.

WHAT COMPETITORS ARE DOING WELL:
- Competitor A has a highly-rated weekend brunch
- Competitor B offers extensive gluten-free options

WHAT COMPETITORS ARE DOING POORLY:
- Competitor A has slow service on weekends

Recommendations:
1. Launch a weekend brunch special to compete with Competitor A
2. Add 2-3 gluten-free menu items this month
3. Consider a happy hour promotion on slow weeknights"""


def test_format_intel_renders_intro_sections_and_recommendations():
    html = str(format_intel(SAMPLE))
    assert "4 nearby competitors" in html
    assert "What competitors are doing well" in html
    assert "Competitor A has a highly-rated weekend brunch" in html
    assert "What competitors are doing poorly" in html
    assert "Recommendations" in html
    assert "Launch a weekend brunch special" in html


def test_format_intel_empty_input_shows_unavailable_message():
    assert "Analysis unavailable" in format_intel("")
    assert "Analysis unavailable" in format_intel(None)


def test_format_intel_body_omits_recommendations():
    html = str(format_intel_body(SAMPLE))
    assert "What competitors are doing well" in html
    assert "What competitors are doing poorly" in html
    assert "Recommendations" not in html
    assert "Launch a weekend brunch special" not in html


def test_format_intel_body_empty_input_returns_empty_markup():
    assert str(format_intel_body("")) == ""
    assert str(format_intel_body(None)) == ""


def test_extract_recs_returns_up_to_three_plain_strings():
    recs = extract_recs(SAMPLE)
    assert recs == [
        "Launch a weekend brunch special to compete with Competitor A",
        "Add 2-3 gluten-free menu items this month",
        "Consider a happy hour promotion on slow weeknights",
    ]


def test_extract_recs_caps_at_three_even_with_more():
    text = "Recommendations:\n1. one\n2. two\n3. three\n4. four\n5. five"
    assert len(extract_recs(text)) == 3


def test_extract_recs_empty_input_returns_empty_list():
    assert extract_recs("") == []
    assert extract_recs(None) == []


def test_extract_recs_requires_explicit_header_not_just_any_numbered_line():
    """The one deliberate behavior difference from format_intel's internal
    recommendation collection: extract_recs anchors strictly to a labeled
    "Recommendations:" header, so a numbered line appearing earlier (inside
    a WELL/POORLY section, before any such header) is not picked up."""
    text = (
        "WHAT COMPETITORS ARE DOING WELL:\n"
        "- Good ambiance\n"
        "1. This stray numbered line shouldn't count\n\n"
        "Recommendations:\n"
        "1. Real recommendation one"
    )
    assert extract_recs(text) == ["Real recommendation one"]


def test_format_intel_handles_markdown_and_em_dashes():
    text = (
        "**Overview** — this area is competitive.\n\n"
        "WHAT COMPETITORS ARE DOING WELL:\n"
        "- **Excellent** food quality — consistently rated 4.5+\n\n"
        "Recommendations:\n"
        "1. **Reduce wait times** by adding a reservation system"
    )
    html = str(format_intel(text))
    assert "*" not in html
    assert "Excellent food quality - consistently rated 4.5+" in html


def test_format_intel_falls_back_to_plain_text_for_unstructured_input():
    text = "Just a plain paragraph with no structure at all."
    html = str(format_intel(text))
    assert "Just a plain paragraph" in html
    assert "What competitors" not in html


def test_format_intel_and_body_agree_on_shared_sections():
    """Both derive from the same parser — whatever bullets one renders for
    well/poorly, the other must render identically (format_intel just adds
    recommendations on top)."""
    full = str(format_intel(SAMPLE))
    body = str(format_intel_body(SAMPLE))
    assert body in full  # body's entire output is a prefix-equivalent subset of full's
