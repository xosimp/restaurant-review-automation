#!/usr/bin/env python3
"""
check_email_tokens.py — keep email HTML on the design system's colours.

The email audit (Sep 19 2026) found 22 client-facing emails built across
three unrelated frames, 14 of them as bespoke inline HTML, each re-typing
the same hex values that already exist as named tokens in emails.BRAND.
DESIGN_SYSTEM.md — the document written specifically to stop web and iOS
drifting apart — contained no reference to email at all, so the surface
clients see most was the one surface nothing governed.

check_colors.py does not help here: its rule is about dark-mode contrast in
templates, and email is deliberately light-mode only (see notify.py's
_alert_email_html, which documents the incident where the dashboard's own
dark-mode switch leaked into alert mail through restaurants.email_theme).

The rule this enforces is narrower and entirely mechanical:

    A hex literal in an email-producing module that is ALREADY a BRAND
    token must be written as that token.

A colour that is not in BRAND is reported but does not fail — some are
genuinely one-off (a tint behind a status pill), and turning every one into
a token is a judgement call, not a lint.

Run: python3 scripts/check_email_tokens.py   (exit 1 on violations)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Modules that build HTML for email. Templates are covered by check_colors.py.
EMAIL_MODULES = ["emails.py", "notify.py", "morning_brief.py", "reporter.py"]

HEX_RE = re.compile(r"#([0-9a-fA-F]{6})\b")

# How many BRAND colours are still written as literals. Ratchet only: this
# number comes down as email HTML is migrated onto the tokens, and the lint
# fails the moment it would go up.
BASELINE = 231

# Lines that legitimately hold a raw hex: the token table itself, and the
# tint map keyed BY those tokens.
SKIP_MARKERS = ("BRAND = {", '"paper":', '"strong":', '"ember":', '"good":',
                "_TINT = {", "BRAND[")


def brand_tokens():
    """{lowercase hex: token name} read from emails.BRAND itself, so the lint
    cannot drift from the palette it is checking against."""
    sys.path.insert(0, ROOT)
    import emails
    return {v.lower(): k for k, v in emails.BRAND.items() if str(v).startswith("#")}


def main():
    tokens = brand_tokens()
    violations, unknown = [], {}

    for name in EMAIL_MODULES:
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
            stripped = line.strip()
            if stripped.startswith("#") or any(m in line for m in SKIP_MARKERS):
                continue
            for found in HEX_RE.findall(line):
                key = "#" + found.lower()
                if key in tokens:
                    violations.append((name, lineno, key, tokens[key], stripped[:88]))
                else:
                    unknown.setdefault(key, []).append(f"{name}:{lineno}")

    if unknown:
        print("email colours outside BRAND (not a failure — review when touching these):")
        for hexval, where in sorted(unknown.items(), key=lambda kv: -len(kv[1]))[:12]:
            print(f"  {hexval}  ×{len(where):<3} {where[0]}")
        print()

    # A RATCHET, not a wall. There are 231 of these today, spread across four
    # modules of email HTML where a careless f-string edit breaks a template
    # nothing renders in CI. Failing outright would force either a risky
    # 231-site rewrite in one go, or a suppression list — and a suppression
    # list is how a lint stops meaning anything.
    #
    # So: the count may never RISE. New email HTML uses the tokens, existing
    # HTML is migrated whenever it is touched, and the number below comes
    # down. It must never go up.
    count = len(violations)
    if count > BASELINE:
        print(f"{count} hex literals duplicate a BRAND token — {count - BASELINE} "
              f"more than the {BASELINE} already here.\n")
        for name, lineno, hexval, token, text in violations[-12:]:
            print(f"  {name}:{lineno}  {hexval} is BRAND[\"{token}\"]")
            print(f"      {text}")
        print("\nUse the token. The palette moves as one thing or it is not a palette.")
        return 1

    if count < BASELINE:
        print(f"email token lint OK — {count} duplicated literals, down from "
              f"{BASELINE}. Lower BASELINE in this file to {count} to hold the gain.")
        return 0

    print(f"email token lint OK — holding at {count} duplicated literals "
          f"(see DESIGN_SYSTEM.md → Email)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
