"""Handled reviews look handled (owner, 10/6/26): "these replied ones need to
be more distinguished from the ones not replied to... the approve button
still looks clickable". A posted, approved or skipped card carries a status
pill, is dimmed and never urgent; the mark-as-answered button says what it
does; a skipped card's Approve is no longer the primary action; the divider
tells answered from skipped."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARD = (ROOT / "templates" / "_review_card.html").read_text(encoding="utf-8")
DASH = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")


def test_the_card_names_its_state_and_is_never_urgent_once_handled():
    assert "{% set _handled = r.response_status in ('posted', 'approved', 'skipped') %}" in CARD
    assert "{{ 'urgent' if r.urgent and not _handled }} {{ 'handled' if _handled }}" in CARD
    assert "{% if r.urgent and not _handled %}<span class=\"rv2-urg\">Urgent</span>{% endif %}" in CARD
    for label in ("'Replied on ' ~ (r.platform|title)", "'Live on ' ~ (r.platform|title)", "\"Couldn't post to Google\"",
                  "'Approved · not on ' ~ (r.platform|title) ~ ' yet'", "'Skipped · no reply sent'"):
        assert label in CARD, label
    assert '<span class="rv2-state {{ _st[0] }}">{{ _st[1] }}</span>' in CARD


def test_the_mark_button_reads_as_an_action_not_a_status():
    assert CARD.count(">Mark as replied on {{ r.platform|title }}</button>") == 3
    assert ">Replied on {{ r.platform|title }}</button>" not in CARD


def test_a_skipped_card_offers_approve_only_as_a_secondary_action():
    sk = CARD[CARD.index("{% elif r.response_status=='skipped' %}"):]
    sk = sk[:sk.index("{% else %}")]
    assert 'class="cbtn cbtn-secondary cbtn-sm" onclick="approveR({{ r.id }},false,true)">✓ Approve after all' in sk
    assert "cbtn-primary" not in sk
    skip_js = DASH[DASH.index("function skipR(id){"):]
    skip_js = skip_js[:skip_js.index("\n}\n")]
    assert "rvSetState(id,'mute','Skipped \\u00b7 no reply sent');" in skip_js and "Approve after all" in skip_js


def test_in_place_changes_keep_the_look_the_server_draws():
    assert ".rv2-row.handled>*{opacity:.6;" in DASH and ".rv2-row.handled .rtext{display:-webkit-box;-webkit-line-clamp:3;" in DASH
    appr = DASH[DASH.index("function approveR(id, confirmed, skipped){"):]
    appr = appr[:appr.index("\n}\n")]
    assert appr.count("rvSetState(id,") == 3
    assert "rvClearState(id);" in DASH[DASH.index("function editApprovedR(id){"):]
