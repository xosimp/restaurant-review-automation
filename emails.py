"""
emails.py — Cavnar AI email sending functions
"""
import os
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", "will@cavnar.ai")


def generate_email_personalization(context: str, fallback: str) -> str:
    """Ask Claude for one short, warm paragraph personalizing an onboarding/
    summary email using the real activity data passed in `context`. Falls
    back to static copy if the API isn't configured or the call fails —
    an email should never fail to send because personalization couldn't
    be generated."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return fallback
    try:
        import anthropic
        from ai_utils import create_with_retry, extract_text
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        prompt = (
            "You are Will, writing a short, warm, genuine paragraph (2-4 sentences) "
            "in a client email for a restaurant using the Cavnar AI dashboard. "
            "Write in first person as Will. No greeting ('Hi X') and no sign-off — "
            "just the paragraph itself, it will be inserted into an existing email. "
            "Reference the specific data given below naturally, not as a list. "
            "Plain text only, no markdown.\n\n" + context
        )
        msg = create_with_retry(
            client,
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=200,
            temperature=0.6,
            messages=[{"role": "user", "content": prompt}],
        )
        text = extract_text(msg).strip()
        return text if text else fallback
    except Exception:
        return fallback

def send_2fa_code(to_email: str, restaurant_name: str, code: str, owner_name: str = None):
    """Send 2FA verification code email."""
    if not RESEND_API_KEY:
        return False
    import requests
    greeting = f"Hi {owner_name}," if owner_name else "Hi,"
    html = f"""
    <div style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:480px;margin:0 auto;background:#f7f4ef;padding:32px 24px;border-radius:12px">
      <div style="text-align:center;margin-bottom:24px">
        <span style="font-family:Georgia,serif;font-size:22px;color:#0e0c0a">Cavnar</span>
        <span style="font-family:Georgia,serif;font-size:22px;color:#c84b2f">AI</span>
      </div>
      <div style="background:white;border-radius:10px;padding:28px 24px;border:1px solid #e0dbd0">
        <p style="color:#3a3530;font-size:15px;margin:0 0 16px">{greeting}</p>
        <p style="color:#3a3530;font-size:15px;margin:0 0 24px">Your verification code for <strong>{restaurant_name}</strong>:</p>
        <div style="text-align:center;margin:24px 0">
          <span style="font-family:monospace;font-size:36px;font-weight:700;letter-spacing:10px;color:#c84b2f;background:#fdf0ef;padding:16px 24px;border-radius:8px;display:inline-block">{code}</span>
        </div>
        <p style="color:#7a736a;font-size:13px;text-align:center;margin:16px 0 0">This code expires in <strong>10 minutes</strong>. If you didn't request this, ignore this email.</p>
      </div>
      <p style="color:#7a736a;font-size:11px;text-align:center;margin-top:20px">Cavnar AI &mdash; Restaurant Intelligence Platform</p>
    </div>
    """
    try:
        resp = requests.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": f"Cavnar AI <{FROM_EMAIL}>", "to": [to_email],
                  "subject": f"Your Cavnar AI verification code: {code}", "html": html},
            timeout=10)
        return resp.status_code == 200
    except Exception:
        return False

def send_login_notification(to_email: str, restaurant_name: str,
                            ip: str = None, user_agent: str = None):
    """Send sign-in notification email."""
    if not RESEND_API_KEY:
        return False
    import requests
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now_str = datetime.now(ZoneInfo("America/Chicago")).strftime("%b %d, %Y at %I:%M %p CT")
    except Exception:
        now_str = datetime.utcnow().strftime("%b %d, %Y at %H:%M UTC")
    # Parse UA into readable string
    ua = user_agent or ""
    if "iPhone" in ua: device = "iPhone"
    elif "iPad" in ua: device = "iPad"
    elif "Android" in ua: device = "Android"
    elif "Windows" in ua: device = "Windows PC"
    elif "Macintosh" in ua or "Mac OS" in ua: device = "Mac"
    else: device = "Unknown device"
    if "Edg/" in ua: browser = "Edge"
    elif "Chrome/" in ua: browser = "Chrome"
    elif "Firefox/" in ua: browser = "Firefox"
    elif "Safari/" in ua: browser = "Safari"
    else: browser = "Browser"
    html = f"""
    <div style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:480px;margin:0 auto;background:#f7f4ef;padding:32px 24px;border-radius:12px">
      <div style="text-align:center;margin-bottom:24px">
        <span style="font-family:Georgia,serif;font-size:22px;color:#0e0c0a">Cavnar</span>
        <span style="font-family:Georgia,serif;font-size:22px;color:#c84b2f">AI</span>
      </div>
      <div style="background:white;border-radius:10px;padding:28px 24px;border:1px solid #e0dbd0">
        <p style="color:#3a3530;font-size:15px;margin:0 0 16px">New sign-in to <strong>{restaurant_name}</strong></p>
        <table style="width:100%;font-size:14px;color:#3a3530;border-collapse:collapse">
          <tr><td style="padding:6px 0;color:#7a736a;width:90px">Time</td><td style="padding:6px 0"><strong>{now_str}</strong></td></tr>
          <tr><td style="padding:6px 0;color:#7a736a">Device</td><td style="padding:6px 0"><strong>{device} &mdash; {browser}</strong></td></tr>
          <tr><td style="padding:6px 0;color:#7a736a">IP address</td><td style="padding:6px 0"><strong>{ip or 'Unknown'}</strong></td></tr>
        </table>
        <p style="color:#7a736a;font-size:13px;margin:20px 0 0;line-height:1.6">If this was you, no action needed. If you don&rsquo;t recognize this sign-in, <a href="mailto:will@cavnar.ai" style="color:#c84b2f">contact Will immediately</a> and change your password.</p>
      </div>
      <p style="color:#7a736a;font-size:11px;text-align:center;margin-top:20px">Cavnar AI &mdash; Restaurant Intelligence Platform</p>
    </div>
    """
    try:
        resp = requests.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": f"Cavnar AI <{FROM_EMAIL}>", "to": [to_email],
                  "subject": f"New sign-in to your Cavnar AI dashboard", "html": html},
            timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


def send_payment_email(to_email, restaurant_name, tier=None,
                       module_count: int = None):
    """Send payment email with a dynamically generated Stripe checkout link."""
    if not RESEND_API_KEY:
        return

    # Determine module count
    if module_count is None:
        tier_counts = {
            "starter_reviews": 1, "starter_labor": 1,
            "starter_inventory": 1, "starter_marketing": 1,
            "full": 4,
        }
        module_count = tier_counts.get(tier, 0)

    if module_count == 0:
        return  # Trial — no payment needed

    setup_price    = f"${module_count * 500:,}"
    retainer_price = f"${module_count * 300:,}/mo"
    label = (
        "1 Module" if module_count == 1 else
        "Full System — 4 Modules" if module_count == 4 else
        f"{module_count} Modules"
    )

    # Generate dynamic Stripe checkout links — both monthly and annual
    checkout_monthly = create_stripe_checkout(module_count, to_email, restaurant_name, "monthly")
    checkout_annual  = create_stripe_checkout(module_count, to_email, restaurant_name, "annual")

    annual_price    = f"${module_count * 3000:,}/yr"
    annual_monthly  = f"${module_count * 250:,}/mo"

    try:
        import resend as _resend
        _resend.api_key = RESEND_API_KEY
        if checkout_monthly and checkout_annual:
            btn_html = f"""
<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:4px">
  <div style="flex:1;min-width:200px;background:white;border:2px solid #c84b2f;border-radius:8px;padding:16px">
    <div style="font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin-bottom:4px">Monthly</div>
    <div style="font-size:20px;font-weight:600;color:#0e0c0a;font-family:Georgia,serif;margin-bottom:2px">{retainer_price}</div>
    <div style="font-size:11px;color:#7a736a;margin-bottom:12px">Cancel anytime with 30 days written notice &nbsp;·&nbsp; No long-term contracts</div>
    <a href="{checkout_monthly}" style="display:block;text-align:center;background:#c84b2f;color:white;padding:10px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">Choose monthly →</a>
  </div>
  <div style="flex:1;min-width:200px;background:#fdf8f6;border:2px solid #2d6a4f;border-radius:8px;padding:16px;position:relative">
    <div style="position:absolute;top:-10px;left:50%;transform:translateX(-50%);background:#2d6a4f;color:white;font-size:10px;font-weight:600;padding:3px 10px;border-radius:20px;white-space:nowrap">2 MONTHS FREE</div>
    <div style="font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin-bottom:4px">Annual</div>
    <div style="font-size:20px;font-weight:600;color:#0e0c0a;font-family:Georgia,serif;margin-bottom:2px">{annual_price}</div>
    <div style="font-size:11px;color:#2d6a4f;font-weight:500;margin-bottom:12px">{annual_monthly}/mo — save ${module_count*600:,}</div>
    <a href="{checkout_annual}" style="display:block;text-align:center;background:#2d6a4f;color:white;padding:10px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">Choose annual →</a>
  </div>
</div>"""
        elif checkout_monthly:
            btn_html = f'<a href="{checkout_monthly}" style="display:inline-block;background:#c84b2f;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;letter-spacing:.04em">Complete payment →</a>'
        else:
            btn_html = '<p style="font-size:13px;color:#3a3530;margin-top:8px">Your payment link will arrive in a separate email shortly.</p>' 
        _resend.Emails.send({
            "from": f"Will Cavnar <{FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"Your Cavnar AI payment link — {restaurant_name}",
            "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <h2 style="font-family:Georgia,serif;font-size:22px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">
      Restaurant Intelligence Dashboard
    </p>
  </div>
  <p style="font-size:15px;line-height:1.6;margin-bottom:8px">
    Hi — excited to get started with <strong>{restaurant_name}</strong>.
    Here is your payment link for the <strong>{label}</strong> plan.
  </p>
  <p style="font-size:14px;color:#3a3530;line-height:1.6;margin-bottom:20px">
    Pick your plan below — {setup_price} setup is the same either way.
    Monthly at {retainer_price}, or save ${module_count*600:,} by going annual.
    30-day free trial on both — no charge until day 31.
  </p>
  <div style="background:#f7f4ef;border-radius:8px;padding:20px 22px;margin-bottom:24px;border-left:3px solid #c84b2f">
    <p style="font-size:11px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin:0 0 6px">{label}</p>
    <div style="display:flex;gap:20px;margin-bottom:14px;flex-wrap:wrap">
      <div>
        <p style="font-size:18px;font-weight:600;color:#0e0c0a;margin:0;font-family:Georgia,serif">{setup_price}</p>
        <p style="font-size:11px;color:#7a736a;margin:0">today</p>
      </div>
      <div style="color:#e0dbd0;font-size:20px;line-height:1.8">+</div>
      <div>
        <p style="font-size:18px;font-weight:600;color:#0e0c0a;margin:0;font-family:Georgia,serif">{retainer_price}</p>
        <p style="font-size:11px;color:#7a736a;margin:0">starting day 31</p>
      </div>
    </div>
    {btn_html}
  </div>
  <p style="font-size:13px;color:#7a736a;line-height:1.6;margin-bottom:24px">
    I'll have your dashboard live within 24 hours of payment clearing.
    Any questions, just reply here.
  </p>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Will Cavnar &nbsp;·&nbsp; Cavnar AI<br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>"""
        })
    except Exception as e:
        print(f"Payment email failed: {e}")

def _log_payment_email(to_email, restaurant_name, module_count):
    try:
        from models import log_email as _log_email, get_conn as _get_conn
        conn = _get_conn()
        row = conn.execute("SELECT id FROM restaurants WHERE owner_email=? LIMIT 1", (to_email,)).fetchone()
        conn.close()
        if row: _log_email(row[0], "payment", to_email, f"Your Cavnar AI payment link — {restaurant_name}")
    except Exception: pass

def send_welcome_email(to_email, restaurant_name, username, password,
                       module_reviews=0, module_labor=0,
                       module_inventory=0, module_marketing=0):
    """Send branded welcome email to new client with their login credentials."""
    import resend as _resend
    _resend.api_key = RESEND_API_KEY
    # Build module list
    active_modules = []
    if module_reviews:  active_modules.append("Review Intelligence")
    if module_labor:    active_modules.append("Labor Optimizer")
    if module_inventory: active_modules.append("Inventory Control")
    if module_marketing: active_modules.append("Marketing Autopilot")
    if not active_modules:
        active_modules = ["Review Intelligence"]  # fallback
    modules_count = len(active_modules)
    if modules_count == 1:
        modules_text = f"one module — {active_modules[0]}"
    else:
        modules_text = f"{modules_count} modules — " + ", ".join(active_modules[:-1]) + f", and {active_modules[-1]}"
    html = f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <h2 style="font-family:Georgia,serif;font-size:22px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">
      Restaurant Intelligence Dashboard
    </p>
  </div>
  <p style="font-size:15px;line-height:1.6;margin-bottom:16px">
    Hi — your Cavnar AI dashboard for <strong>{restaurant_name}</strong> is live and ready to use.
  </p>
  <div style="background:#f7f4ef;border-radius:8px;padding:16px 20px;margin-bottom:20px">
    <p style="font-size:13px;color:#7a736a;margin:0 0 10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">Your login details</p>
    <p style="font-size:14px;margin:0 0 6px"><strong>URL:</strong> <a href="https://dashboard.cavnar.ai" style="color:#c84b2f">dashboard.cavnar.ai</a></p>
    <p style="font-size:14px;margin:0 0 6px"><strong>Username:</strong> {username}</p>
    <p style="font-size:14px;margin:0"><strong>Temporary password:</strong> {password}</p>
  </div>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:12px">
    Once you log in, go to the <strong>Account</strong> tab to set your own password.
    Your dashboard includes {modules_text}, all set up specifically for {restaurant_name}.
  </p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:24px">
    Any questions, just reply to this email. I check it daily.
  </p>
  <p style="font-size:13px;color:#7a736a;line-height:1.6;margin-bottom:24px;padding:10px 14px;background:#f7f4ef;border-radius:6px;border-left:3px solid #c84b2f">
    <strong style="color:#3a3530">Note:</strong> This email may land in your Promotions tab. If it did, drag it to your Primary inbox — that way you won't miss any updates from me going forward.
  </p>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Will Cavnar &nbsp;·&nbsp; Cavnar AI<br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>"""
    _resend.Emails.send({
        "from": f"Will Cavnar <{FROM_EMAIL}>",
        "to": [to_email],
        "subject": f"Your Cavnar AI dashboard is live — {restaurant_name}",
        "html": html,
    })

# ── Routes ────────────────────────────────────────────────────────────────────

def create_stripe_checkout(module_count: int, owner_email: str,
                            restaurant_name: str,
                            billing_period: str = "monthly"):
    """
    Dynamically create a Stripe checkout session for any module count.
    Returns the checkout URL or None on failure.
    Pricing:
      Monthly: $500/module setup (one-time) + $300/mo/module retainer (30-day trial).
      Annual:  $500/module setup (one-time) + $3,000/yr/module retainer (30-day trial).
    """
    import stripe as _stripe
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        print("[STRIPE ERROR] STRIPE_SECRET_KEY not set in environment")
        return None
    if module_count == 0:
        return None

    _stripe.api_key = stripe_key
    setup_amount = module_count * 500 * 100   # in cents (same for both plans)
    # Annual = $3,000/module/yr (equivalent to $250/mo — 2 months free)
    # Monthly = $300/module/mo
    if billing_period == "annual":
        retainer_amount   = module_count * 3000 * 100  # annual in cents
        retainer_interval = "year"
        trial_days        = 30
    else:
        retainer_amount   = module_count * 300 * 100   # monthly in cents
        retainer_interval = "month"
        trial_days        = 30

    try:
        # Ensure products exist (create once, reuse by name)
        def get_or_create_price(product_name, unit_amount, recurring=False, interval="month"):
            # Search for existing product
            products = _stripe.Product.search(query=f'name:"{product_name}"', limit=1)
            if products.data:
                product_id = products.data[0].id
            else:
                product_id = _stripe.Product.create(name=product_name).id

            # Create a fresh price each time (amount may vary)
            kwargs = dict(
                product=product_id,
                unit_amount=unit_amount,
                currency="usd",
            )
            if recurring:
                kwargs["recurring"] = {"interval": interval}
            return _stripe.Price.create(**kwargs).id

        period_label = "Annual" if billing_period == "annual" else "Monthly"
        setup_price_id   = get_or_create_price(
            f"Cavnar AI Setup — {module_count} Module{'s' if module_count>1 else ''}",
            setup_amount
        )
        retainer_price_id = get_or_create_price(
            f"Cavnar AI Retainer {period_label} — {module_count} Module{'s' if module_count>1 else ''}",
            retainer_amount,
            recurring=True,
            interval=retainer_interval
        )

        session = _stripe.checkout.Session.create(
            customer_email=owner_email,
            payment_method_types=["card"],
            line_items=[
                {"price": setup_price_id,    "quantity": 1},
                {"price": retainer_price_id, "quantity": 1},
            ],
            mode="subscription",
            subscription_data={
                "trial_period_days": trial_days,
                "metadata": {
                    "restaurant": restaurant_name,
                    "modules": str(module_count),
                    "billing_period": billing_period,
                }
            },
            success_url="https://dashboard.cavnar.ai?payment=success",
            cancel_url="https://dashboard.cavnar.ai?payment=cancelled",
            custom_text={
                "submit": {"message": f"Pay ${module_count*500} setup today. ${module_count*300}/mo starts in 30 days."}
            },
            metadata={"restaurant": restaurant_name, "modules": str(module_count)},
        )
        return session.url

    except Exception as e:
        import traceback
        print(f"[STRIPE ERROR] Checkout creation failed for {restaurant_name}: {e}")
        traceback.print_exc()
        return None


# ── Onboarding email sequence ─────────────────────────────────────────────────

def send_onboarding_day2(to_email: str, restaurant_name: str, owner_name: str = None,
                          modules: list = None):
    """Day 2 — Getting started: highlight their primary module, not always reviews."""
    if not RESEND_API_KEY:
        return
    try:
        import resend as _resend
        _resend.api_key = RESEND_API_KEY
        first = owner_name.split()[0] if owner_name else "there"
        modules = modules or ["Review Intelligence"]
        modules_text = " and ".join(modules) if len(modules) <= 2 else ", ".join(modules[:-1]) + f", and {modules[-1]}"

        # Build the callout block based on their primary module
        has_reviews   = "Review Intelligence" in modules
        has_labor     = "Labor Optimizer" in modules
        has_inventory = "Inventory Control" in modules
        has_marketing = "Marketing Autopilot" in modules

        if has_reviews:
            callout = """
  <div style="background:#f7f4ef;border-radius:8px;padding:18px 22px;margin-bottom:20px;border-left:3px solid #c84b2f;border-top-left-radius:0;border-bottom-left-radius:0">
    <p style="font-size:14px;color:#0e0c0a;line-height:1.7;margin:0 0 10px">
      <strong>Reviews tab</strong> — Every new review gets pulled in automatically, analyzed for sentiment, and given a suggested response.
      Your job is just to review the draft, edit if needed, and approve it. Takes about 5 minutes a week.
    </p>
    <p style="font-size:13px;color:#7a736a;margin:0">
      Urgent reviews (1-2 stars) show up at the top in red so you never miss one.
    </p>
  </div>"""
        elif has_labor:
            callout = """
  <div style="background:#f7f4ef;border-radius:8px;padding:18px 22px;margin-bottom:20px;border-left:3px solid #c84b2f;border-top-left-radius:0;border-bottom-left-radius:0">
    <p style="font-size:14px;color:#0e0c0a;line-height:1.7;margin:0 0 10px">
      <strong>Labor tab</strong> — Upload your shift schedule CSV and the dashboard will calculate your labor cost percentage, flag overstaffed days, and surface overtime risk automatically.
    </p>
    <p style="font-size:13px;color:#7a736a;margin:0">
      The target is 28-32% labor ratio. The dashboard shows you exactly where you're over and by how much.
    </p>
  </div>"""
        elif has_inventory:
            callout = """
  <div style="background:#f7f4ef;border-radius:8px;padding:18px 22px;margin-bottom:20px;border-left:3px solid #c84b2f;border-top-left-radius:0;border-bottom-left-radius:0">
    <p style="font-size:14px;color:#0e0c0a;line-height:1.7;margin:0 0 10px">
      <strong>Inventory tab</strong> — Upload your weekly inventory count and the dashboard tracks your food cost percentage, flags waste, and gives AI-powered ordering recommendations.
    </p>
    <p style="font-size:13px;color:#7a736a;margin:0">
      The target is 28-32% food cost. You'll see exactly where the money is going.
    </p>
  </div>"""
        elif has_marketing:
            callout = """
  <div style="background:#f7f4ef;border-radius:8px;padding:18px 22px;margin-bottom:20px;border-left:3px solid #c84b2f;border-top-left-radius:0;border-bottom-left-radius:0">
    <p style="font-size:14px;color:#0e0c0a;line-height:1.7;margin:0 0 10px">
      <strong>Marketing tab</strong> — Generate Instagram captions, weekly emails, Google posts, and re-engagement texts in your restaurant's voice in seconds.
    </p>
    <p style="font-size:13px;color:#7a736a;margin:0">
      Just pick a content type, describe what you want to promote, and the AI does the writing.
    </p>
  </div>"""
        else:
            callout = ""

        _resend.Emails.send({
            "from": f"Will Cavnar <{FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"Getting started with your Cavnar AI dashboard",
            "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <h2 style="font-family:Georgia,serif;font-size:22px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">Restaurant Intelligence Dashboard</p>
  </div>
  <p style="font-size:15px;line-height:1.7;margin-bottom:16px">Hi {first} —</p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:16px">
    Your dashboard for <strong>{restaurant_name}</strong> has been live for a day now.
    Here's the most important thing to know about {modules_text}:
  </p>
  {callout}
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:20px">
    Log in anytime at <a href="https://dashboard.cavnar.ai" style="color:#c84b2f;text-decoration:none">dashboard.cavnar.ai</a>.
    If anything looks off or you have questions, just reply here.
  </p>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Will Cavnar &nbsp;·&nbsp; Cavnar AI<br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>"""
        })
        print(f"Onboarding day 2 sent to {to_email}")
    except Exception as e:
        print(f"send_onboarding_day2 failed: {e}")


def send_onboarding_day7(to_email: str, restaurant_name: str, owner_name: str = None,
                          has_labor: bool = False, has_inventory: bool = False,
                          approved_count: int = 0, pending_count: int = 0):
    """Day 7 — First week check-in with real activity data + prompt to upload CSV."""
    if not RESEND_API_KEY:
        return
    try:
        import resend as _resend
        _resend.api_key = RESEND_API_KEY
        first = owner_name.split()[0] if owner_name else "there"

        # Build upload prompt only if they have labor or inventory modules
        upload_block = ""
        if has_labor or has_inventory:
            items = []
            if has_labor:    items.append("shift schedule CSV (export from your POS or scheduling app)")
            if has_inventory: items.append("inventory count CSV")
            items_html = "".join(f"<li style='margin-bottom:6px'>{i}</li>" for i in items)
            upload_block = f"""
  <div style="background:#f7f4ef;border-radius:8px;padding:18px 22px;margin-bottom:20px;border-left:3px solid #c84b2f">
    <p style="font-size:13px;font-weight:600;color:#0e0c0a;margin:0 0 8px;text-transform:uppercase;letter-spacing:.04em">One thing to do this week</p>
    <p style="font-size:14px;color:#3a3530;line-height:1.7;margin:0 0 10px">
      To see your real numbers, upload your data directly in the dashboard — takes about a minute:
    </p>
    <ul style="font-size:13px;color:#3a3530;line-height:1.7;padding-left:18px;margin:0">
      {items_html}
    </ul>
    <p style="font-size:13px;color:#7a736a;margin:10px 0 0">
      Head to your <a href="https://dashboard.cavnar.ai" style="color:#c84b2f;text-decoration:none">dashboard</a>, open the Labor or Inventory tab, and you'll see an upload button at the top. Or just reply here and I'll help you through it.
    </p>
  </div>"""

        # Pre-compute activity sentences (fallback copy if AI personalization fails)
        if approved_count > 0:
            s = "s" if approved_count != 1 else ""
            activity_sentence = f"You've approved {approved_count} review response{s} so far — great start."
        else:
            activity_sentence = "The review monitoring has been running in the background — any new reviews are in your dashboard with draft responses ready."
        if pending_count > 0:
            s = "s" if pending_count != 1 else ""
            pending_sentence = f"You still have {pending_count} review{s} waiting for your approval."
        else:
            pending_sentence = ""
        fallback_paragraph = (
            f"It's been one week since {restaurant_name} went live on Cavnar AI. "
            f"{activity_sentence} {pending_sentence}"
        ).strip()

        ai_context = (
            f"Restaurant: {restaurant_name}. It's been one week since they went live on the dashboard.\n"
            f"Approved review responses so far: {approved_count}.\n"
            f"Reviews still pending their approval: {pending_count}.\n"
            f"Modules: {'Labor Optimizer, ' if has_labor else ''}{'Inventory Control' if has_inventory else ''}\n"
            "Write the one-week check-in paragraph referencing this activity naturally."
        )
        body_paragraph = generate_email_personalization(ai_context, fallback_paragraph)

        _resend.Emails.send({
            "from": f"Will Cavnar <{FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"One week in — how's the dashboard feeling?",
            "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <h2 style="font-family:Georgia,serif;font-size:22px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">Restaurant Intelligence Dashboard</p>
  </div>
  <p style="font-size:15px;line-height:1.7;margin-bottom:16px">Hi {first} —</p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:16px">
    {body_paragraph}
  </p>
  {upload_block}
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:20px">
    Any questions or anything feeling off? Just reply here — I check this daily.
  </p>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Will Cavnar &nbsp;·&nbsp; Cavnar AI<br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>"""
        })
        print(f"Onboarding day 7 sent to {to_email}")
    except Exception as e:
        print(f"send_onboarding_day7 failed: {e}")


def send_reactivation_email(to_email: str, restaurant_name: str, owner_name: str = None,
                             db_path: str = None):
    """Send a welcome-back email when a client is reactivated."""
    if not RESEND_API_KEY:
        return
    try:
        import resend as _resend
        _resend.api_key = RESEND_API_KEY
        first = owner_name.split()[0] if owner_name else "there"
        _resend.Emails.send({
            "from": f"Will Cavnar <{FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"Welcome back to Cavnar AI — {restaurant_name}",
            "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <h2 style="font-family:Georgia,serif;font-size:22px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">Restaurant Intelligence Dashboard</p>
  </div>
  <p style="font-size:15px;line-height:1.7;margin-bottom:16px">Hi {first} —</p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:16px">
    Your <strong>{restaurant_name}</strong> account has been reactivated. Everything is running again —
    review monitoring, your AI modules, and your weekly digest are all back on.
  </p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:24px">
    Jump back into your dashboard whenever you're ready. If anything looks off or you need a refresher, just reply here.
  </p>
  <a href="https://dashboard.cavnar.ai" style="display:inline-block;background:#c84b2f;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;font-family:-apple-system,sans-serif">Go to dashboard →</a>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Questions? Reply to this email or reach me at
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    · <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>"""
        })
    except Exception as e:
        print(f"send_reactivation_email failed: {e}")


def send_monthly_summary_email(to_email: str, restaurant_name: str, owner_name: str = None,
                                restaurant_id: int = None,
                                has_reviews: bool = True, has_labor: bool = False,
                                has_inventory: bool = False, has_marketing: bool = False):
    """Send a monthly summary email with AI-generated insights for the past month."""
    if not RESEND_API_KEY:
        return
    try:
        import resend as _resend
        from datetime import datetime, timedelta
        _resend.api_key = RESEND_API_KEY
        first = owner_name.split()[0] if owner_name else "there"
        now = datetime.now()
        month_name = (now.replace(day=1) - timedelta(days=1)).strftime("%B")  # previous month
        year = (now.replace(day=1) - timedelta(days=1)).year

        # Pull review stats for the month
        review_block = ""
        total = avg = pos = neg = 0
        if has_reviews and restaurant_id:
            try:
                from models import get_reviews_since
                from datetime import timezone
                month_start = now.replace(day=1, hour=0, minute=0, second=0) - timedelta(days=30)
                reviews = get_reviews_since(restaurant_id, month_start.isoformat())
                total = len(reviews)
                if total > 0:
                    avg = round(sum(r.rating for r in reviews) / total, 1)
                    pos = sum(1 for r in reviews if r.rating >= 4)
                    neg = sum(1 for r in reviews if r.rating <= 2)
                    review_block = f"""
  <div style="background:#f5f3f0;border-radius:8px;padding:16px 20px;margin-bottom:16px">
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:#7a736a;margin-bottom:10px">Review Intelligence</div>
    <div style="display:flex;gap:24px;flex-wrap:wrap">
      <div><div style="font-size:28px;font-weight:600;color:#0e0c0a">{total}</div><div style="font-size:11px;color:#7a736a">Total reviews</div></div>
      <div><div style="font-size:28px;font-weight:600;color:#0e0c0a">{avg}★</div><div style="font-size:11px;color:#7a736a">Avg rating</div></div>
      <div><div style="font-size:28px;font-weight:600;color:#2d6a4f">{pos}</div><div style="font-size:11px;color:#7a736a">Positive</div></div>
      <div><div style="font-size:28px;font-weight:600;color:#c84b2f">{neg}</div><div style="font-size:11px;color:#7a736a">Negative</div></div>
    </div>
  </div>"""
            except Exception:
                pass

        # Module summary blocks
        module_blocks = ""
        if has_labor:
            module_blocks += """
  <div style="background:#f5f3f0;border-radius:8px;padding:14px 20px;margin-bottom:12px">
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:#7a736a;margin-bottom:6px">Labor Optimizer</div>
    <p style="font-size:13px;color:#3a3530;margin:0;line-height:1.6">Your labor data has been analyzed all month. Log in to see your latest cost breakdown and schedule recommendations.</p>
  </div>"""
        if has_inventory:
            module_blocks += """
  <div style="background:#f5f3f0;border-radius:8px;padding:14px 20px;margin-bottom:12px">
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:#7a736a;margin-bottom:6px">Inventory Control</div>
    <p style="font-size:13px;color:#3a3530;margin:0;line-height:1.6">Food cost and waste tracking has been running. Check your dashboard for this month's waste report and ordering recommendations.</p>
  </div>"""
        if has_marketing:
            module_blocks += f"""
  <div style="background:#f5f3f0;border-radius:8px;padding:14px 20px;margin-bottom:12px">
    <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:#7a736a;margin-bottom:6px">Marketing Autopilot</div>
    <p style="font-size:13px;color:#3a3530;margin:0;line-height:1.6">Your AI content engine has been ready all month. Log in to generate your content calendar and social posts for {now.strftime("%B")}.</p>
  </div>"""

        # AI-personalized summary paragraph, using the real stats pulled above
        fallback_paragraph = f"Here's a look at how {restaurant_name} performed on Cavnar AI in {month_name}."
        modules_in_use = ", ".join(m for m, on in [
            ("Review Intelligence", has_reviews), ("Labor Optimizer", has_labor),
            ("Inventory Control", has_inventory), ("Marketing Autopilot", has_marketing),
        ] if on) or "Review Intelligence"
        ai_context = (
            f"Restaurant: {restaurant_name}. This is their {month_name} {year} monthly summary email.\n"
            f"Modules in use: {modules_in_use}.\n"
            + (f"Reviews this month: {total} total, {avg}★ average, {pos} positive, {neg} negative.\n" if total else "No new reviews this month.\n")
            + "Write the opening summary paragraph (1-2 sentences) referencing this real data naturally."
        )
        summary_paragraph = generate_email_personalization(ai_context, fallback_paragraph)

        _resend.Emails.send({
            "from": f"Will Cavnar <{FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"{month_name} {year} — your monthly Cavnar AI summary",
            "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <h2 style="font-family:Georgia,serif;font-size:22px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">{month_name} {year} Monthly Summary</p>
  </div>
  <p style="font-size:15px;line-height:1.7;margin-bottom:16px">Hi {first} —</p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:20px">
    {summary_paragraph}
  </p>
  {review_block}
  {module_blocks}
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin:20px 0">
    Log in to your dashboard to see full details, approve any pending review responses, and generate your content for the month ahead.
  </p>
  <a href="https://dashboard.cavnar.ai" style="display:inline-block;background:#c84b2f;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;font-family:-apple-system,sans-serif">View dashboard →</a>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Questions? Reply to this email or reach me at
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    · <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>"""
        })
    except Exception as e:
        print(f"send_monthly_summary_email failed: {e}")


def send_onboarding_day30(to_email: str, restaurant_name: str, owner_name: str = None,
                           modules: list = None, restaurant_id: int = None):
    """Day 30 — 30-day check-in, celebrate milestone, soft feedback ask."""
    if not RESEND_API_KEY:
        return
    try:
        import resend as _resend
        _resend.api_key = RESEND_API_KEY
        first = owner_name.split()[0] if owner_name else "there"
        modules = modules or []

        # Suggest unused modules if they don't have all 4
        all_modules = ["Review Intelligence", "Labor Optimizer", "Inventory Control", "Marketing Autopilot"]
        unused = [m for m in all_modules if m not in modules]
        upsell_block = ""
        if unused:
            unused_text = " and ".join(unused) if len(unused) <= 2 else ", ".join(unused[:-1]) + f", and {unused[-1]}"
            upsell_block = f"""
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:20px">
    One thing worth knowing: you're not currently using <strong>{unused_text}</strong>.
    If you ever want to expand what the dashboard covers, just reply here and I'll walk you through what's included.
  </p>"""

        # Pull real 30-day activity for personalization
        total = avg_rating = responded = 0
        if restaurant_id:
            try:
                from models import get_review_stats
                rstats = get_review_stats(restaurant_id)
                total = rstats.get("total", 0) or 0
                avg_rating = round(rstats.get("avg_rating", 0) or 0, 1)
                responded = rstats.get("responded", 0) or 0
            except Exception:
                pass

        fallback_paragraph = (
            f"{restaurant_name} has been on Cavnar AI for 30 days. "
            "That's a full month of reviews monitored, responses drafted, and data working quietly in the background for you."
        )
        ai_context = (
            f"Restaurant: {restaurant_name}. They've been on the Cavnar AI dashboard for 30 days.\n"
            f"Reviews handled this month: {total}. Responses given: {responded}. Average rating: {avg_rating or 'n/a'}.\n"
            f"Modules in use: {', '.join(modules) if modules else 'Review Intelligence'}.\n"
            "Write the 30-day milestone paragraph referencing this real activity — celebratory but genuine, not over the top."
        )
        body_paragraph = generate_email_personalization(ai_context, fallback_paragraph)

        _resend.Emails.send({
            "from": f"Will Cavnar <{FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"30 days of Cavnar AI — a quick check-in",
            "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <h2 style="font-family:Georgia,serif;font-size:22px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">Restaurant Intelligence Dashboard</p>
  </div>
  <p style="font-size:15px;line-height:1.7;margin-bottom:16px">Hi {first} —</p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:16px">
    {body_paragraph}
  </p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:16px">
    I'd love to hear how it's feeling — is the dashboard saving you time? Anything that could work better?
    A one-line reply is totally fine.
  </p>
  {upsell_block}
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:20px">
    Thanks for being an early client — it genuinely means a lot.
  </p>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Will Cavnar &nbsp;·&nbsp; Cavnar AI<br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>"""
        })
        print(f"Onboarding day 30 sent to {to_email}")
    except Exception as e:
        print(f"send_onboarding_day30 failed: {e}")
