"""Owner round, 9/27/26: one check disc everywhere (and its amber warning),
Why? over the most likely cause, the order-now pills, Send to suppliers
inside Suppliers, the Food Cost hero, the waste trend's selected week, the
Intel rating hero, and AI visibility's re-run and loading state."""
import re

SRC = open("templates/dashboard.html", encoding="utf-8").read()


def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


def test_no_css_or_script_opens_a_jinja_comment():
    # "{#" followed by anything but a space or newline is CSS or JS that
    # Jinja reads as a comment: "{#panel-inventory" swallowed the Marketing
    # and Intel panels whole until the next "#}".
    assert not re.search(r"\{#(?![ \n])", SRC)


# ── one check disc, and its warning ─────────────────────────────────────────

def test_the_disc_tokens_live_on_the_root():
    assert ":root{--hb-pop:#4aae78;" in SRC and "--hb-wpop:" in SRC and "--hb-bpop:" in SRC
    assert "#panel-home,#panel-dsr,#panel-recs{--hb-pop" not in SRC
    for cls in (".cv-ok{", ".cv-warn{", ".cv-bad{", ".cv-ok::before{", ".cv-warn::before{content:'!'}"):
        assert cls in SRC, cls


def test_the_daily_report_uses_the_discs():
    assert ".dr-step.done .ic{background:var(--green-bg)" not in SRC
    assert ".dr-step.gap .ic{background:var(--amber-bg)" not in SRC
    assert "wrItems(w,'win','\\u2713')" not in SRC and "wrItems(r,'risk','!')" not in SRC
    assert "wrItems(w,'win','<i class=\"cv-ok\"></i>')" in SRC
    assert "wrItems(r,'risk','<i class=\"cv-warn\"></i>')" in SRC
    built = _between("function builtHtml(ck,n){", "\n  }\n")
    assert "\\u2713" not in built and "cv-ok cv-md" in built and "cv-warn cv-md" in built
    assert "(it[i].tone==='warn'?'<i class=\"cv-warn\"></i>'" in SRC


def test_no_bare_green_tick_is_left():
    for old in ("<span class=\"ok\">✓</span>", "color:#6fcf97;font-size:10px;font-weight:800\">&#10003;",
                "✓ Available: ", "'<span style=\"color:var(--green)\">✓</span> '", "&#10003; every rule kept",
                "fc2-drift good\">✓", "fc2-drift warn\">⚠", "'<span class=\"im done\">&#10003; done</span>'",
                "{{ '✓' if _tone == 'good'"):
        assert old not in SRC, old
    assert ".in2-gbp .it.done .ic{background:radial-gradient(circle at 35% 30%,var(--hb-pop2),var(--hb-pop) 72%)" in SRC
    assert ".sb-ok{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;color:var(--hb-pop-ink)" in SRC
    assert ".hp-calm-ic{display:flex;align-items:center;justify-content:center;width:44px;height:44px;border-radius:50%;background:radial-gradient" in SRC


# ── Why? over the most likely cause ─────────────────────────────────────────

def test_a_why_inside_a_modal_replaces_it():
    fn = _between("else if(t.hasAttribute('data-explain')){", "else hbAsk(")
    assert "var inModal=t.closest('.cmodal');" in fn
    assert fn.index("cModal.close(inModal.id)") < fn.index("hbExplain(ex[0],ex[1],ex[2])")


# ── Food Cost ───────────────────────────────────────────────────────────────

def test_order_now_quantities_are_red_pills():
    assert ".fc2-col.urgent .row .q .p{background:color-mix(in srgb,var(--hb-bad) 26%,transparent);color:var(--hb-bad)}" in SRC


def test_send_to_suppliers_sits_inside_suppliers():
    sup = _between('<details class="hb-results fc2-work" id="fc2-work-suppliers" hidden>',
                   '<details class="hb-results fc2-work" id="fc2-work-recipes" hidden>')
    assert 'id="fc2-order-send-d"' in sup and 'id="so-body"' in sup
    orders = _between('<div class="fc2-sec" id="fc2-orders">', '<div class="lb2-sig lb2-sig-2">')
    assert 'id="fc2-order-send-d"' not in orders
    # A deep link opens every fold around its section; the order nav both.
    nav = _between("function cavNavSection(p) {", "function cavNavIsPlace(p)")
    assert "while (d) { d.open = true;" in nav
    assert "var sup=document.getElementById('fc2-work-suppliers');if(sup)sup.open=true;" in SRC


def test_the_food_cost_hero_says_nothing_under_its_heading():
    hero = _between('<h1 class="hb-h1">Food cost.', '<div class="fc2-top-right">')
    assert "fc2-h1-basis" not in hero and "dh-badge" not in hero
    assert "@media(min-width:641px){ #panel-inventory .fc2-top-right>.fc2-hero-nums{margin-top:32px}}" in SRC


def test_the_selected_week_is_an_outline_not_a_second_hover():
    hov = _between("function wtHover(i,e){", "\nfunction wtDrawLegend(){")
    assert "bars[b].classList.toggle('on',bi===i);" in hov and "bi===_wt.sel" not in hov
    assert "(on?' sel':'')" in SRC and ".fc2-wt-bar.sel{stroke:var(--ember2);" in SRC


# ── Intel ───────────────────────────────────────────────────────────────────

def test_the_rating_hero_keeps_only_the_standing():
    hero = _between('<section class="lb2-hero in2-rating-hero"', "</section>")
    assert "restaurants nearby" not in hero and "weighted by reviews" not in hero
    assert "standing_basis" not in hero and "standing_why_not" not in hero
    assert "above the block" in hero and "{{ _parts.intro }}" in hero


def test_ai_visibility_reruns_in_view_with_a_big_orb_and_no_card():
    rr = _between("function aivRerunFromTop() {", "\n}\n")
    assert rr.index("runAIVisibility(btn)") < rr.index("scrollIntoView(")
    run = _between("function runAIVisibility(btn) {", "\n}\n")
    assert "cavnarRadarHtml" not in run and "aivLoadingShow();" in run
    assert "document.getElementById('aiv-loading').style.display = 'block'" not in SRC
    assert "var AIV_ORB_SIZE = 260;" in SRC and 'data-orb-state="searching"' in _between("function aivLoadingShow() {", "\n}\n")
    assert "CavnarOrb.destroy(c._orb)" in _between("function aivLoadingHide() {", "\n}\n")
    assert ".in2-pre#aiv-loading{background:none;" in SRC
