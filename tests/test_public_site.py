"""cavnar.ai is a Cloudflare Worker that uploads one directory as static
assets. Until Sep 2026 that directory was the repository root, so the
marketing site served auth.py, models.py, hosted_dashboard.py, .env.example
and .git/index to anyone who guessed a filename. wrangler.jsonc now points
at ./public and these tests keep it that way — an allow-list only holds if
something notices when it stops being one.
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC = os.path.join(ROOT, "public")


def _wrangler():
    """wrangler.jsonc is JSON with // comments — strip them to parse."""
    raw = open(os.path.join(ROOT, "wrangler.jsonc")).read()
    return json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.M))


def test_the_worker_uploads_only_public():
    assert _wrangler()["assets"]["directory"] == "./public", (
        "Widening this back to '.' republishes the whole source tree."
    )


def test_public_holds_the_site_and_nothing_executable():
    served = set()
    for dirpath, _dirs, files in os.walk(PUBLIC):
        for f in files:
            served.add(os.path.relpath(os.path.join(dirpath, f), PUBLIC))

    for page in ("index.html", "pricing.html", "privacy.html", "terms.html", "sitemap.xml"):
        assert page in served, f"{page} is missing from public/"

    # Whatever else lands in public/ later, none of it may be source,
    # secrets, or a database.
    banned = (".py", ".db", ".sqlite", ".pem", ".key", ".env")
    for path in served:
        assert not path.endswith(banned), f"public/{path} would be served on cavnar.ai"
        assert not path.startswith(".git"), f"public/{path} would be served on cavnar.ai"


def test_the_source_tree_is_not_inside_public():
    """The specific files that were public before the fix."""
    for leaked in ("auth.py", "models.py", "hosted_dashboard.py", ".env", ".env.example", "reviews.db"):
        assert not os.path.exists(os.path.join(PUBLIC, leaked)), (
            f"{leaked} is back inside public/ and would be served publicly"
        )


def test_the_two_font_copies_have_not_drifted():
    """public/static/fonts is a copy of static/fonts: the Worker serves the
    marketing site, Flask serves the dashboard, and each needs its own. They
    are byte-identical on purpose — if you change one, change both."""
    app_fonts = os.path.join(ROOT, "static", "fonts")
    site_fonts = os.path.join(PUBLIC, "static", "fonts")
    for name in sorted(os.listdir(app_fonts)):
        a, b = os.path.join(app_fonts, name), os.path.join(site_fonts, name)
        if os.path.isdir(a):
            continue        # static/fonts/pdf: print_forms' TrueType copies, server-side only
        assert os.path.exists(b), f"static/fonts/{name} was never copied into public/"
        assert open(a, "rb").read() == open(b, "rb").read(), (
            f"{name} differs between static/fonts and public/static/fonts"
        )


def test_the_marketing_pages_all_load_the_shared_font_stylesheet():
    """One place to change a font, not four. See project-cavnar-site."""
    for page in ("index.html", "pricing.html", "privacy.html", "terms.html"):
        html = open(os.path.join(PUBLIC, page), encoding="utf-8").read()
        assert "/static/fonts/cavnar-fonts.css" in html, f"{page} is off the shared font stylesheet"
        for retired in ("Cormorant Garamond", "'Syne'", "DM Serif Display", "DM Sans"):
            assert retired not in html, f"{page} still references {retired}"


def test_the_pricing_page_says_what_pricing_py_charges():
    """The redesigned /pricing (10/6/26) states every tier's setup, monthly
    and annual price; pricing.TIERS is the one source, so the page and
    Stripe can never disagree."""
    import pricing
    html = open(os.path.join(PUBLIC, "pricing.html"), encoding="utf-8").read()
    for n, t in pricing.TIERS.items():
        assert f"${t['setup']:,} one-time setup" in html, n
        assert f'data-annual="{t["annual"]:,}" data-monthly="{t["monthly"]:,}"' in html, n


def test_the_site_shares_one_stylesheet_and_script_and_lists_its_pages():
    for page in ("index.html", "pricing.html", "about.html"):
        html = open(os.path.join(PUBLIC, page), encoding="utf-8").read()
        # Versioned (?v=), so a change reaches a browser that already has the file.
        assert re.search(r'<link rel="stylesheet" href="/static/site\.css\?v=[\w.-]+">', html), page
        assert re.search(r'<script src="/static/site\.js\?v=[\w.-]+" defer></script>', html), page
        assert 'href="/#demo"' in html or 'href="#demo"' in html, page
    sitemap = open(os.path.join(PUBLIC, "sitemap.xml"), encoding="utf-8").read()
    for loc in ("https://cavnar.ai/", "https://cavnar.ai/pricing", "https://cavnar.ai/about"):
        assert f"<loc>{loc}</loc>" in sitemap


def test_ask_cavnar_ai_is_shown_as_the_restaurants_cfo_with_labelled_figures():
    """Owner, 10/7/26: Ask Cavnar AI as each restaurant's own CFO, between
    Everything connected and It learns. Its answers are illustrations with
    made-up figures, said so on the panel, and name no client's dishes."""
    html = open(os.path.join(PUBLIC, "index.html"), encoding="utf-8").read()
    assert html.index('id="platform"') < html.index('id="ask"') < html.index('id="h-learn"')
    assert "Illustration · made-up figures" in html
    js = open(os.path.join(PUBLIC, "static", "site.js"), encoding="utf-8").read()
    assert "var QA = [" in js and "smash burger" not in js and "fried rice" not in js
    css = open(os.path.join(PUBLIC, "static", "site.css"), encoding="utf-8").read()
    assert "clip-path:circle(50% at 50% 50%)" in css, "Safari showed the sphere's swirl as a square"


def test_the_ember_core_is_one_renderer_with_a_fallback_and_a_still_frame():
    """The Ember Core (10/7/26): one WebGL script draws the AI at every
    data-core anchor on the homepage; the CSS sphere under each anchor hides
    only once WebGL is live, reduced motion gets a still frame and no
    streams, and the canvas never takes a click."""
    html = open(os.path.join(PUBLIC, "index.html"), encoding="utf-8").read()
    assert re.search(r'<script src="/static/ember-core\.js\?v=[\w.-]+" defer></script>', html)
    for name in ("hero", "prob", "sched", "platform", "ask", "demo"):
        assert f'data-core="{name}"' in html, name
    css = open(os.path.join(PUBLIC, "static", "site.css"), encoding="utf-8").read()
    assert ".core-live [data-core].ember,.core-live [data-core] .ember{visibility:hidden" in css
    # each core paints on its own anchor and scrolls with the page: a canvas
    # fixed over the page trailed iOS's momentum scroll and bounced (10/7/26)
    assert ".core-cv{position:absolute;" in css and "pointer-events:none" in css
    assert "#core-gl" not in css and "position:fixed" not in css.split("#core-streams")[1].split("}")[0]
    js = open(os.path.join(PUBLIC, "static", "ember-core.js"), encoding="utf-8").read()
    assert "prefers-reduced-motion" in js and "root.classList.add('core-live')" in js
    assert "now - lastDraw < 15" in js, "at most 60 frames a second"
    assert "webglcontextlost" in js and "root.classList.remove('core-live')" in js
    assert "=>" not in js and "let " not in js and "const " not in js.replace("const vec", "").replace("const int", ""), "ES5, like site.js"


def test_the_app_and_the_site_run_the_same_ember_core():
    """The dashboard (static/) and cavnar.ai (public/static/) each serve their
    own copy of the Ember Core engine, like the fonts: one AI, one renderer.
    Change one, copy it to the other."""
    a = open(os.path.join(ROOT, "static", "ember-core.js"), encoding="utf-8").read()
    b = open(os.path.join(PUBLIC, "static", "ember-core.js"), encoding="utf-8").read()
    assert a == b, "static/ember-core.js and public/static/ember-core.js have drifted"


def test_the_scroll_story_runs_natively_and_rests_for_reduced_motion():
    """The scroll story (10/8/26): the Ember Thread is drawn in page
    coordinates behind the content (main is lifted above it), so it scrolls
    with the page; the Schedule Generator section is scrubbed by the scroll
    and pins only when it fits; the key words of each headline catch from
    cream to ember. Reduced motion: finished week, drawn thread, warm words."""
    html = open(os.path.join(PUBLIC, "index.html"), encoding="utf-8").read()
    assert 'class="sec scrub" id="schedule"' in html and '<div class="pin">' in html
    assert html.count('class="ig"') >= 6
    assert html.count("data-thread=") == 5
    css = open(os.path.join(PUBLIC, "static", "site.css"), encoding="utf-8").read()
    assert "main{position:relative;z-index:1}" in css
    assert "#thread{position:absolute;" in css and "pointer-events:none;z-index:0" in css
    assert ".sec.scrub.pinned{height:250vh" in css and "prefers-reduced-motion:no-preference" in css
    assert "animation-timeline:view()" in css and ".ig.lit{" in css
    js = open(os.path.join(PUBLIC, "static", "site.js"), encoding="utf-8").read()
    assert "if (reduce) apply(1);" in js, "reduced motion shows the finished week"
    assert "var readY = reduce ? 1e9" in js, "reduced motion shows the thread drawn"
    assert "inner.offsetHeight <= window.innerHeight - 96" in js, "pins only when it fits"
    # the head is a fixed, composited element (drawn in the page and moved by
    # script it showed twice on a fast scroll), and the rail stops where the
    # last branch bends, so nothing pokes past the curve (10/8/26)
    assert ".thread-head{position:fixed;" in css and "head.className = 'thread-head'" in js
    assert "i === taps.length - 1 && t.d ? t.y - 16 : t.y" in js
    assert "=>" not in js
