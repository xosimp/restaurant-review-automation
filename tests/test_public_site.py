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
