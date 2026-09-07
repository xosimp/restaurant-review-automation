"""pricing.py — the one place Cavnar AI's prices live.

pricing.html (the public page) is the source of truth; this mirrors it so
the DocuSign contract tabs, the Stripe checkout amounts, the payment email
and the sales-audit ROI all quote the same numbers. Before this file the
contract and Stripe were still on the launch prices ($500 setup, $300/mo)
while the website said $750 / $349 — a client signing at one price and
paying another is exactly the kind of thing that costs the deal.

Plans (one-time setup at checkout + retainer from day 31, annual saves two months):
  Starter (1 module):  $750 setup · $349/mo · $3,490/yr
  Full System (4):     $3,000 setup · $1,199/mo · $11,990/yr
  2–3 modules are not published; priced as N × Starter until they are.
"""

# Billing terms (Sep 7 2026, Will's call): the setup fee is charged at
# checkout; the retainer — monthly or annual, chosen at checkout — starts
# RETAINER_START_DAYS after setup payment and is billed automatically from
# then on. Either party may cancel with NOTICE_DAYS written notice. The
# contract PDF (scripts/build_contract_pdf.py), the Stripe checkout
# (emails.create_stripe_checkout) and the payment email all read these.
RETAINER_START_DAYS = 30
NOTICE_DAYS = 30

STARTER = {"setup": 750, "monthly": 349, "annual": 3490}
FULL = {"setup": 3000, "monthly": 1199, "annual": 11990}
MODULE_LABELS = {1: "1 Module", 2: "2 Modules", 3: "3 Modules", 4: "Full System — 4 Modules"}


def plan_for(module_count: int) -> dict:
    """setup / monthly / annual dollars for a module count, plus label and
    whether the price is one the public page actually publishes."""
    n = max(0, int(module_count or 0))
    if n == 0:
        return {"modules": 0, "setup": 0, "monthly": 0, "annual": 0, "label": "Trial", "published": True, "plan": "trial"}
    if n >= 4:
        return dict(FULL, modules=4, label=MODULE_LABELS[4], published=True, plan="full")
    return {"modules": n, "setup": STARTER["setup"] * n, "monthly": STARTER["monthly"] * n, "annual": STARTER["annual"] * n,
            "label": MODULE_LABELS.get(n, "%d Modules" % n), "published": n == 1, "plan": "starter"}


def annual_saving(module_count: int) -> int:
    p = plan_for(module_count)
    return p["monthly"] * 12 - p["annual"]


def money(n) -> str:
    return "$" + "{:,.0f}".format(n)
