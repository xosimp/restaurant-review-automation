"""Model output and strangers' text reach the web page as text, never markup.

A review draft is model output shaped by the guest's own words; a review read
is model prose written over reviews; a competitor's reviews and listing come
from Google. Any of them concatenated into innerHTML is a stored XSS a guest
can plant with a review. What these protect:

  * regenDraft builds its markup without the draft and sets the draft with
    textContent / .value (the `else if (boxEl)` branch used to concatenate
    data.draft into a div and a <textarea>);
  * processInsightHtml (the Reviews read) escapes every line;
  * the marketing generator types its copy as text even when it holds a '<';
  * the competitor cards escape the reviews, the name and the vicinity;
  * format_insight_html (the marketing, labor and food reads) escapes the
    model's prose server-side and keeps its own wrapper markup.
The suite has no JS engine, so the template rules read the source.
"""
import os
import re

from client_api import format_insight_html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    with open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def _fn(src, name):
    i = src.index("function %s(" % name)
    j = src.index("\nfunction ", i + 1)
    return src[i:j]


def test_regen_draft_never_concatenates_the_draft_into_innerhtml():
    body = _fn(_src(), "regenDraft")
    # No string concatenation of the draft anywhere in the function...
    assert not re.search(r"\+\s*data\.draft\b", body)
    assert not re.search(r"data\.draft\s*\+", body)
    # ...and it arrives as text in both the shown line and the editor.
    assert "newTxt.textContent = data.draft" in body
    assert "newEditor.value = data.draft" in body
    assert "txtEl.textContent = data.draft" in body
    assert "editorEl.value = data.draft" in body


def test_review_read_lines_are_escaped():
    body = _fn(_src(), "processInsightHtml")
    assert "var ln = _escHtml(lines[_i].trim());" in body
    assert "return out || html;" not in body     # the raw fallback is gone


def test_marketing_copy_is_typed_as_text():
    src = _src()
    assert "typewriterEffect(box, _genContent, true);" in src
    tw = _fn(src, "typewriterEffect")
    assert tw.startswith("function typewriterEffect(el, html, asText)")
    assert "var hasHtml = !asText &&" in tw


def test_competitor_cards_escape_google_text():
    body = _fn(_src(), "loadCompetitorIntel")
    assert "_escHtml(rRaw.length>120" in body
    assert "_escHtml(String(c.name||''))" in body
    assert "_escHtml(String(c.vicinity||''))" in body
    assert "+c.name+" not in body and "+rTxt+" in body


def test_format_insight_html_escapes_model_prose_but_keeps_its_wrapper():
    text = ("Guests said <img src=x onerror=alert(1)> twice.\n"
            "Recommendations:\n"
            "1. Reply to <script>alert(2)</script> today\n"
            "FORECAST: <b>more</b> of the same\n"
            "UNVERIFIED: $<i>9</i>")
    out = format_insight_html(text)
    assert "<img" not in out and "<script" not in out and "<b>" not in out and "<i>" not in out
    assert "&lt;img src=x onerror=alert(1)&gt;" in out
    assert "&lt;script&gt;" in out
    # The function's own markup is still markup.
    assert "<p style=" in out and ">Recommendations</div>" in out and ">Forecast</div>" in out
    assert "Unverified</div>" in out


def test_format_insight_html_escapes_a_read_with_no_recommendations():
    out = format_insight_html("A plain read with a <a href=x>link</a> in it.")
    assert "<a href" not in out and "&lt;a href=x&gt;" in out
