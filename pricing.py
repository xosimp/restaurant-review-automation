"""pricing.py — the one place Cavnar AI's prices live.

pricing.html (the public page) is the source of truth; this mirrors it so
the DocuSign contract tabs, the Stripe checkout amounts, the payment email
and the sales-audit ROI all quote the same numbers. Before this file the
contract and Stripe were still on the launch prices ($500 setup, $300/mo)
while the website said $750 / $349 — a client signing at one price and
paying another is exactly the kind of thing that costs the deal.

Plans (one-time setup at checkout + retainer from day 31, annual saves two months):
  1 module   $750 setup   · $349/mo   · $3,490/yr
  2 modules  $1,500 setup · $649/mo   · $6,490/yr
  3 modules  $2,250 setup · $899/mo   · $8,990/yr
  4 modules  $3,000 setup · $1,199/mo · $11,990/yr

Every one of those is published on pricing.html — the four plan cards carry
1 and 4, the "Can I add or remove modules later?" FAQ carries 2 and 3.

This file used to price 2 and 3 as N × Starter ($698 and $1,047) on the
grounds that they "are not published", which stopped being true the day the
FAQ published them. Nobody noticed, because the only thing that reads these
numbers for 2 or 3 modules is a contract, and no 2- or 3-module contract had
gone out. It would have billed a client $49 or $148 a month more than the
page promised them — the exact failure this file exists to prevent.

The published ladder is a volume curve: $349, then $324.50, $299.67 and
$299.75 per module. N × Starter was a flat $349 with a cliff at 4, which
made three modules a strictly worse buy than four. Annual is 10× monthly at
every tier (two months free). Setup is $750 per module at every tier.
"""

# Billing terms (Sep 7 2026, Will's call): the setup fee is charged at
# checkout; the retainer — monthly or annual, chosen at checkout — starts
# RETAINER_START_DAYS after setup payment and is billed automatically from
# then on. Either party may cancel with NOTICE_DAYS written notice. The
# contract PDF (scripts/build_contract_pdf.py), the Stripe checkout
# (emails.create_stripe_checkout) and the payment email all read these.
RETAINER_START_DAYS = 30
NOTICE_DAYS = 30

# The whole price list, exactly as pricing.html publishes it. Anything that
# quotes, bills or contracts a number reads it from here.
TIERS = {
    1: {"setup": 750,   "monthly": 349,  "annual": 3490},
    2: {"setup": 1500,  "monthly": 649,  "annual": 6490},
    3: {"setup": 2250,  "monthly": 899,  "annual": 8990},
    4: {"setup": 3000,  "monthly": 1199, "annual": 11990},
}

# Kept as names because sales_audit_engine imports them directly.
STARTER = TIERS[1]
FULL = TIERS[4]
MODULE_LABELS = {1: "1 Module", 2: "2 Modules", 3: "3 Modules", 4: "Full System — 4 Modules"}


def plan_for(module_count: int) -> dict:
    """setup / monthly / annual dollars for a module count, plus label and
    whether the price is one the public page actually publishes.

    `published` is True for every real tier now that the page carries all
    four. It stays in the payload because callers branch on it to decide
    whether a figure is safe to put in front of a client, and a count above
    4 still isn't a published price — it's capped at the Full System.
    """
    n = max(0, int(module_count or 0))
    if n == 0:
        return {"modules": 0, "setup": 0, "monthly": 0, "annual": 0, "label": "Trial", "published": True, "plan": "trial"}
    capped = min(n, 4)
    return dict(TIERS[capped], modules=capped, label=MODULE_LABELS[capped],
                published=True, plan="full" if capped == 4 else "starter")


def annual_saving(module_count: int) -> int:
    p = plan_for(module_count)
    return p["monthly"] * 12 - p["annual"]


def money(n) -> str:
    return "$" + "{:,.0f}".format(n)
