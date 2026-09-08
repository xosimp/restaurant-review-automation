"""
emails.py — Cavnar AI email sending functions
"""
import logging
import os
# Read fresh at call time, never bound at import. Binding these at module
# scope meant that importing this module before load_dotenv() ran froze the
# key to "" and silently dropped every send in this file — the same bug
# already fixed in scheduler/admin_routes/audit_app/webhook_routes/mobile_api.
# It happened to work only because hosted_dashboard.py calls load_dotenv()
# before importing us; one import reorder was all it would have taken.
def _resend_key(): return os.getenv("RESEND_API_KEY", "")
def _from_email(): return os.getenv("FROM_EMAIL", "will@cavnar.ai")
log = logging.getLogger(__name__)


def generate_email_personalization(context: str, fallback: str, restaurant_id: int = None,
                                   brief: bool = False) -> str:
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
        # `brief` is for the report emails, which open on ONE line above a stat
        # row that already shows the figures. Left on the default the model
        # writes a 30-word run-up ("I wanted to share some really encouraging
        # news...") and then recites the same numbers the table below is about
        # to show, which is most of what made these read as generated.
        shape = ("a single sentence of no more than 22 words"
                 if brief else "a short, warm, genuine paragraph (2-4 sentences)")
        prompt = (
            f"You are Will, writing {shape} "
            "in a client email for a restaurant using the Cavnar AI dashboard. "
            "Write in first person as Will. No greeting ('Hi X') and no sign-off — "
            "just the text itself, it will be inserted into an existing email. "
            "Reference the specific data given below naturally, not as a list. "
            + ("Open on the substance — no run-up like 'I wanted to share' or "
               "'Great news'. State what happened, plainly. " if brief else "")
            + "Plain text only, no markdown.\n\n" + context
        )
        msg = create_with_retry(
            client,
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
            restaurant_id=restaurant_id,
            action="email_personalization",
        )
        text = extract_text(msg).strip()
        return text if text else fallback
    except Exception:
        return fallback

def send_2fa_code(to_email: str, restaurant_name: str, code: str, owner_name: str = None):
    """Send 2FA verification code email."""
    if not _resend_key():
        log.warning("send_2fa_code: RESEND_API_KEY not set — nothing sent")
        return False
    import requests
    greeting = f"Hi {owner_name}," if owner_name else "Hi,"
    html = f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:480px;margin:0 auto;background:#f7f4ef;padding:32px 24px;border-radius:12px">
      <div style="text-align:center;margin-bottom:24px">
        <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="180" height="32" alt="Cavnar AI" style="display:inline-block;width:180px;height:32px;border:0;outline:none">
      </div>
      <div style="background:white;border-radius:10px;padding:28px 24px;border:1px solid #e0dbd0">
        <p style="color:#3a3530;font-size:15px;margin:0 0 16px">{greeting}</p>
        <p style="color:#3a3530;font-size:15px;margin:0 0 24px">Your verification code for <strong>{restaurant_name}</strong>:</p>
        <div style="text-align:center;margin:24px 0">
          <span style="font-family:ui-monospace,'SF Mono','Space Mono',Menlo,Consolas,monospace;font-size:36px;font-weight:700;letter-spacing:10px;color:#c84b2f;background:#fdf0ef;padding:16px 24px;border-radius:8px;display:inline-block">{code}</span>
        </div>
        <p style="color:#7a736a;font-size:13px;text-align:center;margin:16px 0 0">This code expires in <strong>10 minutes</strong>. If you didn't request this, someone may have your password — change it immediately and contact <a href="mailto:will@cavnar.ai" style="color:#c84b2f">will@cavnar.ai</a>.</p>
      </div>
      <p style="color:#7a736a;font-size:11px;text-align:center;margin-top:20px"><img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:5px;border:0">Cavnar AI &mdash; Restaurant Intelligence Platform</p>
    </div>
</div>
    """
    try:
        _res = deliver(email_type="send_2fa_code", payload={"from": f"Cavnar AI <{_from_email()}>", "to": [to_email],
                  "subject": f"Your Cavnar AI verification code: {code}", "html": _html_document(html)})
        return _res
    except Exception as e:
        log.warning("send_2fa_code: request to Resend failed: %s", e)
        return False

def send_login_notification(to_email: str, restaurant_name: str,
                            ip: str = None, user_agent: str = None, report_url: str = None):
    """Send sign-in notification email."""
    if not _resend_key():
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
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:480px;margin:0 auto;background:#f7f4ef;padding:32px 24px;border-radius:12px">
      <div style="text-align:center;margin-bottom:24px">
        <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="180" height="32" alt="Cavnar AI" style="display:inline-block;width:180px;height:32px;border:0;outline:none">
      </div>
      <div style="background:white;border-radius:10px;padding:28px 24px;border:1px solid #e0dbd0">
        <p style="color:#3a3530;font-size:15px;margin:0 0 16px">New sign-in to <strong>{restaurant_name}</strong></p>
        <table style="width:100%;font-size:14px;color:#3a3530;border-collapse:collapse">
          <tr><td style="padding:6px 0;color:#7a736a;width:90px">Time</td><td style="padding:6px 0"><strong>{now_str}</strong></td></tr>
          <tr><td style="padding:6px 0;color:#7a736a">Device</td><td style="padding:6px 0"><strong>{device} &mdash; {browser}</strong></td></tr>
          <tr><td style="padding:6px 0;color:#7a736a">IP address</td><td style="padding:6px 0"><strong>{ip or 'Unknown'}</strong></td></tr>
        </table>
        <p style="color:#7a736a;font-size:13px;margin:20px 0 0;line-height:1.6">If this was you, no action needed.</p>
        {("<p style=\"margin:16px 0 0\"><a href=\"" + report_url + "\" style=\"display:inline-block;background:#c84b2f;color:#fff;text-decoration:none;font-weight:700;font-size:14px;padding:11px 18px;border-radius:8px\">This wasn&rsquo;t me</a></p><p style=\"color:#7a736a;font-size:12px;margin:10px 0 0;line-height:1.6\">That link signs out every device, forgets every remembered device, and requires a password reset before anyone can sign in again. It works once, for 7 days.</p>") if report_url else "<p style=\"color:#7a736a;font-size:13px;margin:8px 0 0;line-height:1.6\">If you don&rsquo;t recognize this sign-in, <a href=\"mailto:will@cavnar.ai\" style=\"color:#c84b2f\">contact Will immediately</a> and change your password.</p>"}
      </div>
      <p style="color:#7a736a;font-size:11px;text-align:center;margin-top:20px"><img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:5px;border:0">Cavnar AI &mdash; Restaurant Intelligence Platform</p>
    </div>
</div>
    """
    try:
        _res = deliver(email_type="send_login_notification", payload={"from": f"Cavnar AI <{_from_email()}>", "to": [to_email],
                  "subject": f"New sign-in to your Cavnar AI dashboard", "html": _html_document(html)})
        return _res
    except Exception as e:
        log.warning(f"send_login_notification error: {e}")
        return False


# ── Delivery core ───────────────────────────────────────────────────────────

class SendResult:
    """What actually happened to one send.

    Every sender used to return a bare bool that no call site read, so a
    failed 2FA code, staff schedule or supplier order was indistinguishable
    from a delivered one — and email_log recorded 'sent' either way. This
    carries enough to log the truth and to let a caller react.
    """
    __slots__ = ("ok", "message_id", "error", "status_code", "attempts")

    def __init__(self, ok, message_id=None, error=None, status_code=None, attempts=1):
        self.ok = ok
        self.message_id = message_id
        self.error = error
        self.status_code = status_code
        self.attempts = attempts

    def __bool__(self):
        """Back-compatible with `if send_x(...)` and with the old bool
        returns, so existing call sites keep working unchanged."""
        return bool(self.ok)

    def __repr__(self):
        return f"<SendResult ok={self.ok} status={self.status_code} attempts={self.attempts} err={self.error!r}>"


# 429 and 5xx are worth another go; 4xx (bad address, unverified domain) is
# not — retrying those just burns time and makes the same mistake three times.
_RETRY_STATUS = {408, 429, 500, 502, 503, 504}

# A suppressed address still gets security mail — locking someone out of
# their own account because a newsletter bounced would be a worse failure
# than the one suppression is protecting against.
_SUPPRESSION_EXEMPT = {
    "send_2fa_code",
    "send_password_reset_email",
    "send_password_reset_code_email",
    "send_password_changed_email",
    "send_email_changed_email",
    "send_recovery_email_code",
    "send_login_notification",
}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5   # 0.5s, 1s — deliberately short; these run inline in
                      # request handlers and scheduler ticks, not a queue.


def deliver(payload: dict = None, restaurant_id=None, email_type=None, log_send: bool = True) -> SendResult:
    """Send one email through Resend, with retry on transient failures, and
    record the real outcome in email_log.

    Single choke point on purpose: retry, logging and status all used to be
    absent, and adding them at 17 separate call sites would have guaranteed
    they drifted.
    """
    import time as _time
    import requests as _requests

    key = _resend_key()
    to = payload.get("to")
    to_email = (to[0] if isinstance(to, (list, tuple)) and to else to) or ""
    subject = payload.get("subject", "")

    if not key or not to_email:
        result = SendResult(False, error="RESEND_API_KEY or recipient missing", attempts=0)
        _record(restaurant_id, email_type, to_email, subject, result, log_send)
        return result

    # Flood guard for code-style transactional mail. A retry loop, a stuck
    # client, or a script hammering login could otherwise mail one address
    # dozens of times an hour and burn the account's daily quota — which
    # is exactly what the test suite did once. Marketing and digests are
    # naturally bounded; only the types that are triggered per request
    # are capped here, per recipient, on a rolling window.
    if not _flood_guard_ok(email_type, to_email):
        result = SendResult(False, error="flood guard: too many %s emails to %s in the last hour" % (email_type, to_email), attempts=0)
        _record(restaurant_id, email_type, to_email, subject, result, log_send)
        log.warning(result.error)
        return result

    # Marketing mail carries a working opt-out; CAN-SPAM requires one and
    # until now none of these four had any. Applied centrally so a new
    # marketing template can't be added without it.
    if email_type in _MARKETING_TYPES and restaurant_id:
        payload = _add_unsubscribe(payload, restaurant_id)

    # Suppression is enforced here rather than at call sites so a bounced or
    # complained address is dropped no matter which of the 26 senders fires.
    # Security mail is exempt: someone whose marketing bounced must still be
    # able to receive a 2FA code or a password reset.
    if email_type not in _SUPPRESSION_EXEMPT:
        try:
            from models import is_email_suppressed
            if is_email_suppressed(to_email):
                result = SendResult(False, error="recipient suppressed (bounced/complained)", attempts=0)
                _record(restaurant_id, email_type, to_email, subject, result, log_send)
                return result
        except Exception as e:
            log.warning("suppression check failed for %s: %s", to_email, e)

    last = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = _requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload, timeout=15,
            )
            if resp.status_code == 200:
                mid = None
                try:
                    mid = (resp.json() or {}).get("id")
                except Exception:
                    pass
                result = SendResult(True, message_id=mid, status_code=200, attempts=attempt)
                _record(restaurant_id, email_type, to_email, subject, result, log_send)
                return result

            last = SendResult(False, error=(resp.text or "")[:300],
                              status_code=resp.status_code, attempts=attempt)
            if resp.status_code not in _RETRY_STATUS:
                break
        except Exception as e:
            last = SendResult(False, error=str(e)[:300], attempts=attempt)

        if attempt < _MAX_ATTEMPTS:
            _time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))

    # `last or ...` would be wrong here: SendResult.__bool__ reports .ok, so a
    # failed result is falsy and would be silently replaced by the fallback,
    # throwing away the real status code, body and attempt count.
    result = last if last is not None else SendResult(False, error="unknown send failure")
    log.warning("email %r to %s failed after %s attempt(s): %s",
                subject, to_email, result.attempts, result.error)
    _record(restaurant_id, email_type, to_email, subject, result, log_send)
    return result


# The four templates that are product marketing rather than transactional.
# Everything else — receipts, codes, schedules, supplier orders, alerts — is
# mail the recipient asked for by using the product, and must not carry an
# unsubscribe that would silently turn off operational email.
_MARKETING_TYPES = {
    "send_onboarding_day2",
    "send_onboarding_day7",
    "send_onboarding_day30",
    "send_monthly_summary_email",
}


def _add_unsubscribe(payload: dict, restaurant_id: int) -> dict:
    """Append a visible footer link and the one-click headers.

    List-Unsubscribe-Post is what lets Gmail/Apple render their own native
    "Unsubscribe" affordance instead of routing people to the spam button —
    which is the outcome that actually damages a sending domain.
    """
    try:
        from models import unsubscribe_token
        base = (os.getenv("BASE_URL") or "https://dashboard.cavnar.ai").rstrip("/")
        url = f"{base}/u/{unsubscribe_token(restaurant_id)}"
    except Exception as e:
        log.warning("unsubscribe link build failed for restaurant %s: %s", restaurant_id, e)
        return payload

    footer = (
        '<div style="text-align:center;margin:18px auto 0;max-width:560px;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Helvetica Neue\',Arial,sans-serif">'
        '<p style="font-size:11px;color:#9a9088;line-height:1.6;margin:0">'
        'You get this because you use Cavnar AI. '
        f'<a href="{url}" style="color:#9a9088;text-decoration:underline">Unsubscribe from product emails</a>.'
        '<br>Account and security emails are sent regardless.'
        '</p></div>'
    )
    html = payload.get("html") or ""
    if "</body>" in html:
        html = html.replace("</body>", footer + "</body>", 1)
    else:
        html += footer

    out = dict(payload)
    out["html"] = html
    out["headers"] = {
        **(payload.get("headers") or {}),
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    return out


# email_type -> (max sends, window seconds) per recipient. Anything not listed
# is uncapped. Windows are rolling and read from email_log, so the guard
# also holds across worker restarts.
FLOOD_LIMITS = {
    "send_2fa_code": (8, 3600),
    "send_login_notification": (8, 3600),
    "send_password_reset_email": (6, 3600),
    "send_password_reset_code_email": (6, 3600),
    "send_recovery_email_code": (6, 3600),
    "send_email_changed_email": (6, 3600),
    "send_password_changed_email": (6, 3600),
    "send_team_invite_email": (10, 3600),
    "digest_preview": (6, 3600),
}


def _flood_guard_ok(email_type, to_email):
    """True when this send is under the per-recipient cap for its type.
    Fails open: a logging-table hiccup must never block a real 2FA code."""
    lim = FLOOD_LIMITS.get(email_type or "")
    if not lim or not to_email:
        return True
    max_n, window = lim
    try:
        from models import get_conn
        # email_log.sent_at is written in America/Chicago local time (see
        # models.log_email), so the window start must be computed the same way.
        from datetime import datetime, timedelta
        try:
            import zoneinfo
            now_local = datetime.now(zoneinfo.ZoneInfo("America/Chicago")).replace(tzinfo=None)
        except Exception:
            now_local = datetime.now()
        since = (now_local - timedelta(seconds=window)).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_conn()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM email_log WHERE email_type=? AND lower(to_email)=lower(?) "
                "AND status='sent' AND sent_at >= ?",
                (email_type, to_email, since)).fetchone()[0]
        finally:
            conn.close()
        return n < max_n
    except Exception:
        return True


def _record(restaurant_id, email_type, to_email, subject, result: SendResult, log_send: bool):
    """Best-effort logging — a logging failure must never turn a delivered
    email into an exception at the call site."""
    if not log_send or not email_type:
        return
    try:
        from models import log_email
        log_email(restaurant_id, email_type, to_email, subject,
                  status=("sent" if result.ok else "failed"),
                  error=result.error, message_id=result.message_id)
    except Exception as e:
        log.warning("email_log write failed for %r: %s", subject, e)


_MODULE_DISPLAY_NAMES = {
    "reviews": "Review Intelligence",
    "labor": "Labor Optimizer",
    "inventory": "Food Cost Control",
    "marketing": "Marketing Autopilot",
}


def _module_display_names(modules) -> list:
    """Accept either the display names the scheduler passes ("Review
    Intelligence") or the short module keys used everywhere else in the
    codebase ("reviews"), and return display names.

    These templates branch on `"Review Intelligence" in modules`, so a
    caller handing over keys silently matched nothing and rendered an
    email with its whole middle section missing. Normalising here means
    the templates can only be wrong about a module the restaurant genuinely
    does not have.
    """
    out = []
    for m in (modules or []):
        name = _MODULE_DISPLAY_NAMES.get(str(m).strip().lower(), str(m).strip())
        if name and name not in out:
            out.append(name)
    return out


def _clock(value) -> str:
    """"16:00" -> "4:00 PM". Staff schedules were going out in 24-hour time,
    which nobody on a floor in the US reads. Anything that isn't a plain
    HH:MM is passed through untouched rather than mangled."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        hh, mm = raw.split(":")[:2]
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return raw
    except (ValueError, TypeError):
        return raw
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def restaurant_usage(restaurant_id) -> dict:
    """What this restaurant has ACTUALLY done, per module they own.

    The lifecycle emails used to talk about usage without ever looking:
    day 30 computed "you're not currently using X" as
    `all_four_modules - modules_they_bought`, so it named modules the
    client had never purchased and told them they weren't using them. And
    day 7's "one thing to do this week" was a fixed string about uploading
    a CSV, which is advice most clients can't act on — they're connected to
    a POS from onboarding, so there is no CSV to upload.

    Every value here is read from this restaurant's own rows, so an email
    can say something true or say nothing.
    """
    usage = {
        "owns": {}, "used": {},
        "pending_reviews": 0, "approved_reviews": 0,
        "pos": None, "has_schedule": False, "ingredient_count": 0,
    }
    if not restaurant_id:
        return usage
    try:
        from models import get_conn, get_restaurant
        r = get_restaurant(restaurant_id)
        if not r:
            return usage
        usage["owns"] = {
            "reviews": bool(r.module_reviews), "labor": bool(r.module_labor),
            "inventory": bool(r.module_inventory), "marketing": bool(r.module_marketing),
        }
        for label, token in (("Toast", getattr(r, "toast_access_token", None)),
                             ("Square", getattr(r, "square_access_token", None)),
                             ("Clover", getattr(r, "clover_api_token", None))):
            if token:
                usage["pos"] = label
                break

        conn = get_conn()
        try:
            def scalar(sql, args):
                row = conn.execute(sql, args).fetchone()
                return (row[0] if row else 0) or 0

            usage["approved_reviews"] = scalar(
                "SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                "AND response_status IN ('approved','posted')", (restaurant_id,))
            usage["pending_reviews"] = scalar(
                "SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                "AND response_status='drafted' AND draft_response IS NOT NULL "
                "AND TRIM(draft_response) != ''", (restaurant_id,))
            usage["has_schedule"] = scalar(
                "SELECT COUNT(*) FROM schedule_history WHERE restaurant_id=?", (restaurant_id,)) > 0
            usage["ingredient_count"] = scalar(
                "SELECT COUNT(*) FROM ingredients WHERE restaurant_id=?", (restaurant_id,))
            has_shifts = scalar(
                "SELECT COUNT(*) FROM client_data WHERE restaurant_id=? AND shifts_csv IS NOT NULL "
                "AND TRIM(shifts_csv) != ''", (restaurant_id,)) > 0
            marketing_used = scalar(
                "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=?", (restaurant_id,)) > 0
        finally:
            conn.close()

        usage["used"] = {
            "reviews": usage["approved_reviews"] > 0,
            "labor": usage["has_schedule"] or has_shifts or usage["pos"] is not None,
            "inventory": usage["ingredient_count"] > 0,
            "marketing": marketing_used,
        }
    except Exception as e:
        log.warning("restaurant_usage failed for %s: %s", restaurant_id, e)
    return usage


def _html_document(fragment: str, bg: str = "#f7f4ef") -> str:
    """Wraps an email's inner markup in a real HTML document (doctype, head,
    body). Every template in this file used to hand Resend a bare <div>
    fragment with no <html>/<body> — the fragment's own background only
    ever extended as tall as its own content, so any mail client whose
    message viewport is taller than that (nearly all of them) showed its
    own default body color in the leftover space: usually white, but black
    in a client running in dark mode, exactly the cut-off-halfway look
    these emails had. Setting the background on a real <body> tag makes it
    fill the whole message viewport the way client-side page backgrounds
    normally do. The color-scheme meta tags stop Gmail/Apple Mail's dark
    mode from re-theming (or inverting) an email that was deliberately
    designed as a light card, which is the same failure mode from a
    different angle.

    A background on <body> alone was NOT enough, which is why several of
    these still showed as "only half the screen": a body with no height
    only grows as tall as its content, so the client's own default colour
    still filled everything below it. Height has to be claimed explicitly
    all the way down (html -> body -> a 100%-height presentation table),
    which is the long-standing bulletproof-email answer to this and the
    only one Gmail, Outlook and Apple Mail all honour."""
    # Idempotent. reporter.py builds a whole document and its five callers
    # then wrapped it in this one, nesting <!doctype><html> inside a <td>.
    stripped = (fragment or "").lstrip().lower()
    if stripped.startswith("<!doctype") or stripped.startswith("<html"):
        return fragment
    return f"""<!doctype html>
<html style="height:100%;margin:0;padding:0;background:{bg}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;height:100%;width:100%;background:{bg}">
<table role="presentation" width="100%" height="100%" cellpadding="0" cellspacing="0" border="0" style="background:{bg};height:100%;width:100%;margin:0;padding:0;border-collapse:collapse">
  <tr>
    <td valign="top" style="background:{bg};padding:0">
{fragment}
    </td>
  </tr>
</table>
</body>
</html>"""


# Public alias. The wrapper originally lived here as a private helper and so
# only ever got applied to this file's own templates — every other module
# that talks to Resend directly (ops, scheduler, admin_routes, webhook_routes,
# client_api, notify, auth_routes, mobile_api, audit_app, reporter) kept
# handing over a bare fragment and kept showing the cut-off background. They
# import this.
html_document = _html_document


def _branded_email(inner_html: str) -> str:
    """The shared full-bleed wrapper + wordmark header + seal footer every
    client-facing email here uses — for the two new self-serve emails below
    so they match the rest without re-pasting the frame."""
    return _html_document(f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:480px;margin:0 auto;background:#f7f4ef;padding:32px 24px;border-radius:12px">
      <div style="text-align:center;margin-bottom:24px">
        <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="180" height="32" alt="Cavnar AI" style="display:inline-block;width:180px;height:32px;border:0;outline:none">
      </div>
      <div style="background:white;border-radius:10px;padding:28px 24px;border:1px solid #e0dbd0">
        {inner_html}
      </div>
      <p style="color:#7a736a;font-size:11px;text-align:center;margin-top:20px"><img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:5px;border:0">Cavnar AI &mdash; Restaurant Intelligence Platform</p>
    </div>
</div>
""")


def _send_branded(to_email: str, subject: str, inner_html: str, from_label: str = "Cavnar AI") -> bool:
    return deliver(email_type="send_login_notification", payload={"from": f"{from_label} <{_from_email()}>", "to": [to_email],
                    "subject": subject, "html": _branded_email(inner_html)})


# ── Report-email kit ───────────────────────────────────────────────────────
# The monthly summary and the weekly digest are the only two emails Cavnar AI
# sends that REPORT numbers rather than announce one thing, and both had
# drifted away from everything else: the digest into a dark theme (silently
# inherited from the web dashboard's own dark-mode switch) and five nested
# bordered cards, the summary into `display:flex` stat rows — which Outlook
# and parts of Gmail don't implement, so they collapsed into a ragged stack —
# plus a paragraph of generic prose per module.
#
# This is the one layout they now share: a single card with hairline rules
# instead of nested borders, stat rows built out of real <table> cells,
# numbers in Space Grotesk per the house rule, and ember spent in exactly
# three places (the card's top rule, the one action, the button).
BRAND = {
    "paper": "#f7f4ef", "card": "#ffffff", "border": "#e0dbd0", "rule": "#ece7dd",
    "strong": "#0e0c0a", "ink": "#1a1714", "body": "#4a443d", "muted": "#7a736a",
    "ember": "#c84b2f", "ember2": "#e8956a",
    "good": "#2d6a4f", "warn": "#a8681c", "bad": "#c0392b",
}
_SANS = "-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif"
_NUM = "'Space Grotesk','Helvetica Neue',Arial,sans-serif"
_TINT = {
    BRAND["good"]: "#e7f0ea", BRAND["warn"]: "#f6eddf",
    BRAND["bad"]: "#f8e9e7", BRAND["ember"]: "#fbf1ec",
}
_WORDMARK = "https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png"
_SEAL = "https://dashboard.cavnar.ai/static/brand/seal-dark-email.png"


def report_rule(space: int = 20) -> str:
    """A hairline. This is what every one of those nested bordered cards was
    trying to do, at five times the visual weight."""
    return f'<div style="border-top:1px solid {BRAND["rule"]};margin:{space}px 0"></div>'


def report_eyebrow(label: str, color: str = None, tag: str = None,
                   tag_color: str = None) -> str:
    """Section label, with an optional status pill on the right."""
    right = ""
    if tag:
        tc = tag_color or BRAND["muted"]
        right = (f'<td align="right" valign="middle"><span style="font-family:{_SANS};font-size:10px;'
                 f'font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:{tc};'
                 f'background:{_TINT.get(tc, "#f2efe9")};padding:3px 9px;border-radius:20px;'
                 f'white-space:nowrap">{tag}</span></td>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="border-collapse:collapse;margin:0 0 12px"><tr>'
            f'<td valign="middle"><span style="font-family:{_SANS};font-size:10px;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:.11em;color:{color or BRAND["muted"]}">'
            f'{label}</span></td>{right}</tr></table>')


def report_stats(stats) -> str:
    """A stat row as real table cells — the thing `display:flex` was pretending
    to be. Rows of up to four; every figure is set in Space Grotesk."""
    stats = [s for s in stats if s]
    if not stats:
        return ""
    # Balanced rows. A straight chunk-of-four left five figures as 4 + 1, and
    # the orphan read like a mistake; five goes 3 + 2 instead.
    n = len(stats)
    per = n if n <= 4 else -(-n // -(-n // 4))
    per = min(per, 4) or 1
    chunks = [stats[i:i + per] for i in range(0, n, per)]
    rows = ""
    for ci, chunk in enumerate(chunks):
        w = 100 // len(chunk)
        cells = ""
        for entry in chunk:
            value, label = entry[0], entry[1]
            color = entry[2] if len(entry) > 2 and entry[2] else BRAND["strong"]
            cells += (f'<td width="{w}%" align="left" valign="top" style="padding:0 12px 0 0">'
                      f'<div style="font-family:{_NUM};font-size:25px;font-weight:700;line-height:1.1;'
                      f'color:{color}">{value}</div>'
                      f'<div style="font-family:{_SANS};font-size:10px;text-transform:uppercase;'
                      f'letter-spacing:.07em;color:{BRAND["muted"]};margin-top:5px">{label}</div></td>')
        # Pad the short row so its cells keep the same width as the row above.
        cells += ('<td width="%d%%"></td>' % w) * (per - len(chunk))
        rows += f"<tr>{cells}</tr>"
        if ci + 1 < len(chunks):
            rows += f'<tr><td colspan="{per}" style="height:18px;font-size:0;line-height:0">&nbsp;</td></tr>'
    # table-layout:fixed, or the browser ignores the per-cell widths and lets
    # whichever figure has the longest label eat the row.
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="border-collapse:collapse;table-layout:fixed">{rows}</table>')


def report_lines(lines) -> str:
    """LABEL + one true sentence, on a quiet left rule. Replaces the
    paragraph-per-module blocks, which said the same generic thing to every
    restaurant whether or not it was true of them."""
    out = ""
    for label, text in lines:
        if not text:
            continue
        out += (f'<div style="border-left:2px solid {BRAND["border"]};padding:1px 0 1px 13px;'
                f'margin:0 0 14px">'
                f'<div style="font-family:{_SANS};font-size:10px;font-weight:700;'
                f'text-transform:uppercase;letter-spacing:.08em;color:{BRAND["muted"]}">{label}</div>'
                f'<div style="font-family:{_SANS};font-size:13.5px;color:{BRAND["body"]};'
                f'line-height:1.55;margin-top:4px">{text}</div></div>')
    return out.rstrip()


def report_quote(name: str, meta: str, text: str, accent: str) -> str:
    return (f'<div style="border-left:2px solid {accent};padding:1px 0 1px 13px;margin:0 0 14px">'
            f'<div style="font-family:{_SANS};font-size:12px;font-weight:600;color:{BRAND["ink"]}">'
            f'{name}<span style="font-family:{_NUM};font-weight:700;color:{accent};'
            f'margin-left:7px">{meta}</span></div>'
            f'<div style="font-family:{_SANS};font-size:13px;color:{BRAND["body"]};line-height:1.55;'
            f'margin-top:4px">&ldquo;{text}&rdquo;</div></div>')


def report_action(label: str, text: str) -> str:
    """The single do-this-next line — the one loud thing on the page."""
    return (f'<div style="background:{_TINT[BRAND["ember"]]};border-left:3px solid {BRAND["ember"]};'
            f'border-radius:0 8px 8px 0;padding:15px 17px">'
            + report_eyebrow(label, BRAND["ember"]) +
            f'<div style="font-family:{_SANS};font-size:14px;font-weight:600;color:{BRAND["strong"]};'
            f'line-height:1.55">{text}</div></div>')


def report_paragraph(text: str) -> str:
    return (f'<p style="font-family:{_SANS};font-size:14.5px;color:{BRAND["ink"]};line-height:1.65;'
            f'margin:0">{text}</p>')


def report_shell(kicker: str, title: str, subtitle: str, sections,
                 cta_label: str = None, cta_url: str = "https://dashboard.cavnar.ai") -> str:
    body = report_rule().join(s for s in sections if s)
    cta = ""
    if cta_label:
        cta = (f'<div style="margin-top:26px"><a href="{cta_url}" style="display:block;'
               f'background:{BRAND["ember"]};color:#ffffff;text-align:center;padding:14px 20px;'
               f'border-radius:8px;text-decoration:none;font-family:{_SANS};font-size:13px;'
               f'font-weight:600;letter-spacing:.03em">{cta_label}</a></div>')
    return _html_document(f"""
<div style="background:{BRAND['paper']};width:100%;padding:36px 20px;box-sizing:border-box">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;border-collapse:collapse">
  <tr>
    <td valign="middle"><img src="{_WORDMARK}" width="150" height="26" alt="Cavnar AI" style="display:block;width:150px;height:26px;border:0;outline:none"></td>
    <td valign="middle" align="right"><span style="font-family:{_SANS};font-size:10px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:{BRAND['muted']}">{kicker}</span></td>
  </tr>
  <tr><td colspan="2" style="padding-top:16px">
    <div style="background:{BRAND['card']};border:1px solid {BRAND['border']};border-top:3px solid {BRAND['ember']};border-radius:12px;padding:28px 26px">
      <h1 style="font-family:{_SANS};font-size:21px;font-weight:700;letter-spacing:-.01em;color:{BRAND['strong']};margin:0">{title}</h1>
      <div style="font-family:{_SANS};font-size:12px;color:{BRAND['muted']};margin-top:6px">{subtitle}</div>
      {report_rule(22)}
      {body}
      {cta}
    </div>
  </td></tr>
  <tr><td colspan="2" align="center" style="padding-top:18px">
    <p style="font-family:{_SANS};font-size:11px;color:{BRAND['muted']};margin:0;text-align:center"><img src="{_SEAL}" width="13" height="13" alt="" style="vertical-align:middle;margin-right:6px;border:0"><span style="vertical-align:middle">Cavnar AI &nbsp;&middot;&nbsp; <a href="mailto:will@cavnar.ai" style="color:{BRAND['muted']};text-decoration:none">will@cavnar.ai</a> &nbsp;&middot;&nbsp; <a href="https://cavnar.ai" style="color:{BRAND['muted']};text-decoration:none">cavnar.ai</a></span></p>
  </td></tr>
</table>
</td></tr>
</table>
</div>""")


def send_password_reset_email(to_email: str, reset_url: str) -> bool:
    """The app's Forgot Password flow. Same 1-hour link the web page's own
    inline version sends — this is that email, factored out so the mobile
    endpoint doesn't carry a second copy of the HTML."""
    return _send_branded(to_email, "Reset your Cavnar AI password", f"""
        <p style="color:#0e0c0a;font-size:18px;font-weight:700;margin:0 0 12px">Reset your password</p>
        <p style="color:#3a3530;font-size:14px;line-height:1.6;margin:0 0 24px">Tap the button to choose a new password. This link expires in 1 hour.</p>
        <a href="{reset_url}" style="display:inline-block;background:#c84b2f;color:white;padding:12px 28px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600">Reset password &rarr;</a>
        <p style="color:#7a736a;font-size:12px;margin:24px 0 0;line-height:1.6">If you didn't request this, ignore this email &mdash; your password won't change.</p>
    """)


def send_password_reset_code_email(to_email: str, code: str) -> bool:
    """The app's in-app reset: a 6-digit code typed into the sheet, instead
    of the web flow's emailed link. Expires with the same 1-hour window."""
    return _send_branded(to_email, "Your Cavnar AI reset code", f"""
        <p style="color:#0e0c0a;font-size:18px;font-weight:700;margin:0 0 12px">Reset your password</p>
        <p style="color:#3a3530;font-size:14px;line-height:1.6;margin:0 0 20px">Enter this code in the app to choose a new password. It expires in 1 hour.</p>
        <div style="text-align:center;margin:0 0 20px">
          <span style="font-family:ui-monospace,'SF Mono','Space Mono',Menlo,Consolas,monospace;font-size:32px;font-weight:700;letter-spacing:8px;white-space:nowrap;color:#c84b2f;background:#fdf0ef;padding:14px 20px;border-radius:8px;display:inline-block">{code}</span>
        </div>
        <p style="color:#7a736a;font-size:12px;margin:0;line-height:1.6">If you didn't request this, someone may have your email address on file &mdash; your password won't change unless this code is used. Contact <a href="mailto:will@cavnar.ai" style="color:#c84b2f">will@cavnar.ai</a> if you're concerned.</p>
    """)


def send_signup_welcome_email(to_email: str, restaurant_name: str, owner_name: str = None) -> bool:
    """Self-serve signup from the app. Deliberately NOT send_welcome_email —
    that one prints the temporary password Will assigns an onboarded client;
    a person who just typed their own password never needs it emailed
    back to them in plaintext."""
    first = (owner_name or "").split()[0] if owner_name else ""
    greet = f"Hi {first}," if first else "Hi,"
    return _send_branded(to_email, f"Welcome to Cavnar AI — {restaurant_name}", f"""
        <p style="color:#0e0c0a;font-size:18px;font-weight:700;margin:0 0 12px">{greet} your account is ready.</p>
        <p style="color:#3a3530;font-size:14px;line-height:1.6;margin:0 0 16px"><strong>{restaurant_name}</strong> is set up on Cavnar AI with a free trial of every module — Reviews, Labor, Food Cost, and Marketing. You're already signed in on the app; the same login works on the web at <a href="https://dashboard.cavnar.ai" style="color:#c84b2f">dashboard.cavnar.ai</a>.</p>
        <p style="color:#3a3530;font-size:14px;line-height:1.6;margin:0 0 24px">First thing worth doing: connect Google Business under Account &rarr; Connected apps, so reviews start flowing in.</p>
        <a href="https://dashboard.cavnar.ai" style="display:inline-block;background:#c84b2f;color:white;padding:12px 28px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600">Open your dashboard &rarr;</a>
        <p style="color:#7a736a;font-size:12px;margin:24px 0 0;line-height:1.6">Questions? Reply to this email or reach Will at <a href="mailto:will@cavnar.ai" style="color:#c84b2f">will@cavnar.ai</a>.</p>
    """)


def send_signup_admin_alert(restaurant_name: str, owner_name: str, email: str, phone: str = None) -> bool:
    """Heads-up to Will the moment someone self-registers — every other new
    client so far has been created by hand, so a signup nobody set up
    shouldn't be discovered days later in the admin list."""
    to = os.getenv("WILL_EMAIL", "will@cavnar.ai")
    return _send_branded(to, f"New signup — {restaurant_name}", f"""
        <p style="color:#0e0c0a;font-size:18px;font-weight:700;margin:0 0 12px">New self-serve signup</p>
        <table style="width:100%;font-size:14px;color:#3a3530;border-collapse:collapse">
          <tr><td style="padding:6px 0;color:#7a736a;width:110px">Restaurant</td><td style="padding:6px 0"><strong>{restaurant_name}</strong></td></tr>
          <tr><td style="padding:6px 0;color:#7a736a">Owner</td><td style="padding:6px 0"><strong>{owner_name or '—'}</strong></td></tr>
          <tr><td style="padding:6px 0;color:#7a736a">Email</td><td style="padding:6px 0"><strong>{email}</strong></td></tr>
          <tr><td style="padding:6px 0;color:#7a736a">Phone</td><td style="padding:6px 0"><strong>{phone or '—'}</strong></td></tr>
        </table>
        <p style="color:#7a736a;font-size:12px;margin:20px 0 0;line-height:1.6">Created on the trial tier with all four modules on. Review it in the <a href="https://dashboard.cavnar.ai/admin" style="color:#c84b2f">admin panel</a>.</p>
    """, from_label="Cavnar AI Alerts")


def send_payment_email(to_email, restaurant_name, tier=None,
                       module_count: int = None):
    """Send payment email with a dynamically generated Stripe checkout link."""
    if not _resend_key():
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

    from pricing import plan_for, annual_saving, money as _pm
    plan = plan_for(module_count)
    setup_price    = _pm(plan["setup"])
    retainer_price = f"{_pm(plan['monthly'])}/mo"
    label = plan["label"]
    saving = annual_saving(module_count)

    # Generate dynamic Stripe checkout links — both monthly and annual
    checkout_monthly = create_stripe_checkout(module_count, to_email, restaurant_name, "monthly")
    checkout_annual  = create_stripe_checkout(module_count, to_email, restaurant_name, "annual")

    annual_price    = f"{_pm(plan['annual'])}/yr"
    annual_monthly  = f"{_pm(round(plan['annual'] / 12.0))}/mo"

    try:
        if checkout_monthly and checkout_annual:
            btn_html = f"""
<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:4px">
  <div style="flex:1;min-width:200px;background:white;border:2px solid #c84b2f;border-radius:8px;padding:16px">
    <div style="font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin-bottom:4px">Monthly</div>
    <div style="font-size:20px;font-weight:600;color:#0e0c0a;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;margin-bottom:2px">{retainer_price}</div>
    <div style="font-size:11px;color:#7a736a;margin-bottom:12px">Billed monthly from day 31 &nbsp;·&nbsp; Cancel with 30 days' written notice</div>
    <a href="{checkout_monthly}" style="display:block;text-align:center;background:#c84b2f;color:white;padding:10px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">Choose monthly →</a>
  </div>
  <div style="flex:1;min-width:200px;background:#fdf8f6;border:2px solid #2d6a4f;border-radius:8px;padding:16px;position:relative">
    <div style="position:absolute;top:-10px;left:50%;transform:translateX(-50%);background:#2d6a4f;color:white;font-size:10px;font-weight:600;padding:3px 10px;border-radius:20px;white-space:nowrap">2 MONTHS FREE</div>
    <div style="font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin-bottom:4px">Annual</div>
    <div style="font-size:20px;font-weight:600;color:#0e0c0a;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;margin-bottom:2px">{annual_price}</div>
    <div style="font-size:11px;color:#2d6a4f;font-weight:500;margin-bottom:12px">{annual_monthly} equivalent — save ${saving:,} &nbsp;·&nbsp; billed once on day 31</div>
    <a href="{checkout_annual}" style="display:block;text-align:center;background:#2d6a4f;color:white;padding:10px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">Choose annual →</a>
  </div>
</div>"""
        elif checkout_monthly:
            btn_html = f'<a href="{checkout_monthly}" style="display:inline-block;background:#c84b2f;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;letter-spacing:.04em">Complete payment →</a>'
        else:
            btn_html = '<p style="font-size:13px;color:#3a3530;margin-top:8px">Your payment link will arrive in a separate email shortly.</p>' 
        deliver(email_type="send_payment_email", payload={
            "from": f"Will Cavnar <{_from_email()}>",
            "to": [to_email],
            "subject": f"Your Cavnar AI payment link — {restaurant_name}",
            "html": _html_document(f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:560px;margin:0 auto;color:#1a1714;background:#f7f4ef;border-radius:12px;padding:32px 24px;box-sizing:border-box">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="170" height="30" alt="Cavnar AI" style="display:block;width:170px;height:30px;border:0;outline:none;margin:0 0 6px">
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">
      Restaurant Intelligence Dashboard
    </p>
  </div>
  <p style="font-size:15px;line-height:1.6;margin-bottom:8px">
    Hi — excited to get started with <strong>{restaurant_name}</strong>.
    Here is your payment link for the <strong>{label}</strong> plan.
  </p>
  <p style="font-size:14px;color:#3a3530;line-height:1.6;margin-bottom:20px">
    Pick your plan below — {setup_price} setup is the same either way and is billed today.
    Monthly at {retainer_price}, or save ${saving:,} by going annual.
    The retainer starts 30 days after setup, and you can cancel with 30 days' written notice.
  </p>
  <div style="background:#f7f4ef;border-radius:8px;padding:20px 22px;margin-bottom:24px;border-left:3px solid #c84b2f">
    <p style="font-size:11px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin:0 0 6px">{label}</p>
    <div style="display:flex;gap:20px;margin-bottom:14px;flex-wrap:wrap">
      <div>
        <p style="font-size:18px;font-weight:600;color:#0e0c0a;margin:0;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif">{setup_price}</p>
        <p style="font-size:11px;color:#7a736a;margin:0">today</p>
      </div>
      <div style="color:#e0dbd0;font-size:20px;line-height:1.8">+</div>
      <div>
        <p style="font-size:18px;font-weight:600;color:#0e0c0a;margin:0;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif">{retainer_price}</p>
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
    <img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:6px;border:0"><span style="vertical-align:middle">Will Cavnar &nbsp;·&nbsp; Cavnar AI</span><br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>
</div>""")
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
    # Build module list
    active_modules = []
    if module_reviews:  active_modules.append("Review Intelligence")
    if module_labor:    active_modules.append("Labor Optimizer")
    if module_inventory: active_modules.append("Food Cost Control")
    if module_marketing: active_modules.append("Marketing Autopilot")
    if not active_modules:
        active_modules = ["Review Intelligence"]  # fallback
    modules_count = len(active_modules)
    if modules_count == 1:
        modules_text = f"one module — {active_modules[0]}"
    else:
        modules_text = f"{modules_count} modules — " + ", ".join(active_modules[:-1]) + f", and {active_modules[-1]}"
    html = f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:560px;margin:0 auto;color:#1a1714;background:#f7f4ef;border-radius:12px;padding:32px 24px;box-sizing:border-box">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="170" height="30" alt="Cavnar AI" style="display:block;width:170px;height:30px;border:0;outline:none;margin:0 0 6px">
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
    <img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:6px;border:0"><span style="vertical-align:middle">Will Cavnar &nbsp;·&nbsp; Cavnar AI</span><br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>
</div>"""
    deliver(email_type="send_welcome_email", payload={
        "from": f"Will Cavnar <{_from_email()}>",
        "to": [to_email],
        "subject": f"Your Cavnar AI dashboard is live — {restaurant_name}",
        "html": _html_document(html),
    })


def send_staff_schedule_email(to_email, employee_name, restaurant_name, week_label,
                              link, shifts, reply_to=None):
    """One employee's shifts plus their own link.

    The shifts are in the email body on purpose — staff read this on a
    phone between jobs, and making them tap through to find out whether
    they work Tuesday defeats the point. The link is for the always-current
    version, since a schedule can change after it's sent."""
    if shifts:
        rows = "".join(
            f'''<tr>
      <td style="padding:9px 12px;border-bottom:1px solid #e0dbd0;font-size:14px;white-space:nowrap"><strong>{s.get("day") or s.get("date","")}</strong></td>
      <td style="padding:9px 12px;border-bottom:1px solid #e0dbd0;font-size:14px;color:#1a1714;white-space:nowrap">{_clock(s.get("start") or s.get("shift_start"))} – {_clock(s.get("end") or s.get("shift_end"))}</td>
      <td style="padding:9px 12px;border-bottom:1px solid #e0dbd0;font-size:13px;color:#7a736a">{s.get("role","")}</td>
    </tr>'''
            for s in shifts
        )
        body = f'''<table style="width:100%;border-collapse:collapse;margin:0 0 18px">{rows}</table>'''
    else:
        body = ('<p style="font-size:15px;line-height:1.6;margin:0 0 18px">'
                "You're not scheduled for any shifts this week.</p>")

    html = f"""
<div style="background:#ffffff;width:100%;padding:32px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:520px;margin:0 auto;color:#1a1714">
  <p style="font-size:15px;line-height:1.6;margin:0 0 4px">Hi {employee_name},</p>
  <p style="font-size:15px;line-height:1.6;margin:0 0 18px">
    Here's your schedule at <strong>{restaurant_name}</strong> for {week_label}.
  </p>
  {body}
  <p style="margin:0 0 22px">
    <a href="{link}" style="display:inline-block;background:#c84b2f;color:#ffffff;text-decoration:none;font-weight:700;font-size:14px;padding:11px 18px;border-radius:8px">View my schedule</a>
  </p>
  <p style="font-size:13px;color:#7a736a;line-height:1.6;margin:0">
    That link always shows your current shifts, so check it if anything changes.
    Questions about a shift go to your manager.
  </p>
</div></div>
"""
    _etype = "send_staff_schedule_email"
    params = {
        # The restaurant is the visible sender — staff know the restaurant,
        # not Cavnar.
        "from": f"{restaurant_name} <{_from_email()}>",
        "to": [to_email],
        "subject": f"Your schedule — {week_label}",
        "html": _html_document(html),
    }
    if reply_to:
        params["reply_to"] = reply_to
    return deliver(params, email_type=_etype)


def send_supplier_order_email(to_email, supplier_name, restaurant_name, po_number,
                              items, total_cost, reply_to=None):
    """The suggested order, sent to the supplier who actually fills it.

    Deliberately plain and scannable — a supplier reads this on a phone in
    a warehouse, so it's a quantity table and a PO number, not a branded
    marketing layout. `reply_to` is the restaurant's own address so the
    supplier replies to the restaurant, not to Cavnar."""
    rows = "".join(
        f'''<tr>
      <td style="padding:8px 12px;border-bottom:1px solid #e0dbd0;font-size:14px;color:#1a1714">{i.get("item") or i.get("name") or ""}</td>
      <td style="padding:8px 12px;border-bottom:1px solid #e0dbd0;font-size:14px;color:#1a1714;text-align:right;white-space:nowrap"><strong>{i.get("qty",0)}</strong> {i.get("unit","")}</td>
    </tr>'''
        for i in (items or [])
    )
    greeting = f"Hi {supplier_name}," if supplier_name else "Hi,"
    html = f"""
<div style="background:#ffffff;width:100%;padding:32px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <p style="font-size:15px;line-height:1.6;margin:0 0 4px">{greeting}</p>
  <p style="font-size:15px;line-height:1.6;margin:0 0 20px">
    Please supply the following for <strong>{restaurant_name}</strong>.
  </p>
  <p style="font-size:13px;color:#7a736a;margin:0 0 10px;letter-spacing:1px;text-transform:uppercase;font-weight:600">
    Order {po_number}
  </p>
  <table style="width:100%;border-collapse:collapse;margin-bottom:18px">
    <thead>
      <tr>
        <th align="left" style="padding:8px 12px;border-bottom:2px solid #1a1714;font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#7a736a">Item</th>
        <th align="right" style="padding:8px 12px;border-bottom:2px solid #1a1714;font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#7a736a">Quantity</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="font-size:14px;color:#3a3530;margin:0 0 24px">
    Please confirm availability and delivery date by replying to this email.
  </p>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Sent by {restaurant_name} via Cavnar AI. Reference {po_number} on the invoice.
  </p>
</div></div>
"""
    _etype = "send_supplier_order_email"
    params = {
        # The supplier knows the restaurant, not Cavnar — so the restaurant
        # is the visible sender, on the same verified _from_email() domain
        # every other send in this file uses.
        "from": f"{restaurant_name} <{_from_email()}>",
        "to": [to_email],
        "subject": f"Order {po_number} — {restaurant_name}",
        "html": _html_document(html),
    }
    if reply_to:
        params["reply_to"] = reply_to
    return deliver(params, email_type=_etype)


def send_team_invite_email(to_email, restaurant_name, username, password, inviter_name=None):
    """Self-serve team-invite counterpart to send_welcome_email() above —
    same credentials-in-an-email shape (matches the risk profile already
    accepted for every restaurant's primary login), reworded for "added
    to an existing dashboard" instead of "your dashboard is live"."""
    added_by = f" by {inviter_name}" if inviter_name else ""
    html = f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:560px;margin:0 auto;color:#1a1714;background:#f7f4ef;border-radius:12px;padding:32px 24px;box-sizing:border-box">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="170" height="30" alt="Cavnar AI" style="display:block;width:170px;height:30px;border:0;outline:none;margin:0 0 6px">
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">
      Restaurant Intelligence Dashboard
    </p>
  </div>
  <p style="font-size:15px;line-height:1.6;margin-bottom:16px">
    You've been added{added_by} to the Cavnar AI dashboard for <strong>{restaurant_name}</strong>.
  </p>
  <div style="background:#f7f4ef;border-radius:8px;padding:16px 20px;margin-bottom:20px">
    <p style="font-size:13px;color:#7a736a;margin:0 0 10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">Your login details</p>
    <p style="font-size:14px;margin:0 0 6px"><strong>URL:</strong> <a href="https://dashboard.cavnar.ai" style="color:#c84b2f">dashboard.cavnar.ai</a></p>
    <p style="font-size:14px;margin:0 0 6px"><strong>Username:</strong> {username}</p>
    <p style="font-size:14px;margin:0"><strong>Temporary password:</strong> {password}</p>
  </div>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:24px">
    Once you log in, go to the <strong>Account</strong> tab to set your own password.
  </p>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    <img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:6px;border:0"><span style="vertical-align:middle">Will Cavnar &nbsp;·&nbsp; Cavnar AI</span><br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
  </p>
</div>
</div>"""
    deliver(email_type="send_team_invite_email", payload={
        "from": f"Will Cavnar <{_from_email()}>",
        "to": [to_email],
        "subject": f"You've been added to {restaurant_name}'s Cavnar AI dashboard",
        "html": _html_document(html),
    })

# ── Routes ────────────────────────────────────────────────────────────────────

def create_stripe_checkout(module_count: int, owner_email: str,
                            restaurant_name: str,
                            billing_period: str = "monthly"):
    """
    Dynamically create a Stripe checkout session for any module count.
    Returns the checkout URL or None on failure.
    Pricing comes from pricing.py (mirrors pricing.html): Starter $750 setup +
    $349/mo or $3,490/yr per module; Full System $3,000 setup + $1,199/mo or
    $11,990/yr. Prices are created fresh on every checkout, so changing
    pricing.py is the whole update — nothing to edit in the Stripe dashboard.
    """
    import stripe as _stripe
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        print("[STRIPE ERROR] STRIPE_SECRET_KEY not set in environment")
        return None
    if module_count == 0:
        return None

    _stripe.api_key = stripe_key
    from pricing import plan_for
    from pricing import money, RETAINER_START_DAYS
    plan = plan_for(module_count)
    setup_amount = plan["setup"] * 100   # cents, same for both billing periods
    if billing_period == "annual":
        retainer_amount   = plan["annual"] * 100
        retainer_interval = "year"
    else:
        retainer_amount   = plan["monthly"] * 100
        retainer_interval = "month"

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
                # The setup fee (one-time line item) is charged at checkout;
                # the retainer — monthly or annual — starts on day 31. This
                # is the contract's term (pricing.RETAINER_START_DAYS), not
                # a marketing trial, and applies to both billing periods.
                "trial_period_days": RETAINER_START_DAYS,
                "metadata": {
                    "restaurant": restaurant_name,
                    "modules": str(module_count),
                    "billing_period": billing_period,
                }
            },
            success_url="https://dashboard.cavnar.ai?payment=success",
            cancel_url="https://dashboard.cavnar.ai?payment=cancelled",
            custom_text={
                "submit": {"message": f"{money(plan['setup'])} setup today. Your {'annual' if billing_period == 'annual' else 'monthly'} retainer of {money(plan['annual'] if billing_period == 'annual' else plan['monthly'])} starts in {RETAINER_START_DAYS} days."}
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
                          modules: list = None, restaurant_id: int = None):
    """Day 2 — Getting started: highlight their primary module, not always reviews."""
    if not _resend_key():
        return
    try:
        first = owner_name.split()[0] if owner_name else "there"
        modules = _module_display_names(modules) or ["Review Intelligence"]
        modules_text = " and ".join(modules) if len(modules) <= 2 else ", ".join(modules[:-1]) + f", and {modules[-1]}"

        # Build the callout block based on their primary module
        has_reviews   = "Review Intelligence" in modules
        has_labor     = "Labor Optimizer" in modules
        has_inventory = "Food Cost Control" in modules
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
      The target is 33-36% labor ratio. The dashboard shows you exactly where you're over and by how much.
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

        # The lead-in only exists if something follows it. It used to be
        # unconditional, so any module list that matched none of the four
        # branches above rendered "Here's the most important thing to know
        # about ...:" and then simply stopped — a colon promising a payload
        # that was never there.
        lead_in = (f"\n    Here's the most important thing to know about {modules_text}:"
                   if callout else "")

        deliver(email_type="send_onboarding_day2", restaurant_id=restaurant_id, payload={
            "from": f"Will Cavnar <{_from_email()}>",
            "to": [to_email],
            "subject": f"Getting started with your Cavnar AI dashboard",
            "html": _html_document(f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:560px;margin:0 auto;color:#1a1714;background:#f7f4ef;border-radius:12px;padding:32px 24px;box-sizing:border-box">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="170" height="30" alt="Cavnar AI" style="display:block;width:170px;height:30px;border:0;outline:none;margin:0 0 6px">
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">Restaurant Intelligence Dashboard</p>
  </div>
  <p style="font-size:15px;line-height:1.7;margin-bottom:16px">Hi {first} —</p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:16px">
    Your dashboard for <strong>{restaurant_name}</strong> has been live for a day now.{lead_in}
  </p>
  {callout}
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:20px">
    Log in anytime at <a href="https://dashboard.cavnar.ai" style="color:#c84b2f;text-decoration:none">dashboard.cavnar.ai</a>.
    If anything looks off or you have questions, just reply here.
  </p>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    <img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:6px;border:0"><span style="vertical-align:middle">Will Cavnar &nbsp;·&nbsp; Cavnar AI</span><br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>
</div>""")
        })
        print(f"Onboarding day 2 sent to {to_email}")
    except Exception as e:
        print(f"send_onboarding_day2 failed: {e}")


def send_onboarding_day7(to_email: str, restaurant_name: str, owner_name: str = None,
                          has_labor: bool = False, has_inventory: bool = False,
                          approved_count: int = 0, pending_count: int = 0,
                          restaurant_id: int = None):
    """Day 7 — first-week check-in, with ONE next step chosen from what
    this restaurant has actually done so far (see restaurant_usage)."""
    if not _resend_key():
        return
    try:
        first = owner_name.split()[0] if owner_name else "there"

        # ONE next step, chosen from what this restaurant has actually done.
        #
        # This used to be a fixed "upload your shift schedule CSV / inventory
        # count CSV" block shown to everyone with those modules. Most clients
        # are connected to a POS during onboarding, so there is no CSV to
        # upload and the advice was something they either couldn't act on or
        # had already done — which is exactly what makes an email read as
        # generated rather than written. Now it looks first and only asks for
        # something genuinely outstanding; if nothing is, it says nothing.
        usage = restaurant_usage(restaurant_id)
        owns, used = usage.get("owns") or {}, usage.get("used") or {}
        pos = usage.get("pos")

        action_title, action_body = None, None
        if owns.get("reviews", has_labor is not None) and usage.get("pending_reviews", 0) > 0:
            n = usage["pending_reviews"]
            action_title = "One thing to do this week"
            action_body = (
                f"You have <strong>{n} repl{'y' if n == 1 else 'ies'}</strong> drafted and waiting for your OK. "
                "Open Reviews, read them, and approve the ones you're happy with — it's the whole job, "
                "and it takes about five minutes."
            )
        elif has_labor and owns.get("labor") and not usage.get("has_schedule"):
            action_title = "One thing to do this week"
            action_body = (
                (f"Your {pos} data is already flowing in. " if pos else "")
                + "Open Labor and generate next week's schedule — it builds from your own sales and shift "
                  "history, and you can move anything you don't like before it goes out."
            ) if pos else (
                "Open Labor and connect your POS (Toast, Square or Clover) so the schedule can build from "
                "your own sales history. It's under Account &rarr; Connections and takes a minute."
            )
        elif has_inventory and owns.get("inventory") and usage.get("ingredient_count", 0) == 0:
            action_title = "One thing to do this week"
            action_body = (
                "Open Food Cost and add the fifteen or twenty items you actually buy most weeks. "
                "That's enough for it to start flagging waste and telling you what to reorder — "
                "you don't need to count the whole walk-in."
            )
        elif owns.get("marketing") and not used.get("marketing"):
            action_title = "One thing to do this week"
            action_body = (
                "Open Marketing and generate one post. It writes in your restaurant's voice from what's "
                "already on your profile, so the first one takes about thirty seconds to approve."
            )

        upload_block = ""
        if action_title:
            upload_block = f"""
  <div style="background:#f7f4ef;border-radius:8px;padding:18px 22px;margin-bottom:20px;border-left:3px solid #c84b2f">
    <p style="font-size:13px;font-weight:600;color:#0e0c0a;margin:0 0 8px;text-transform:uppercase;letter-spacing:.04em">{action_title}</p>
    <p style="font-size:14px;color:#3a3530;line-height:1.7;margin:0">
      {action_body}
    </p>
    <p style="font-size:13px;color:#7a736a;margin:10px 0 0">
      It's all in the <a href="https://dashboard.cavnar.ai" style="color:#c84b2f;text-decoration:none">dashboard</a> — or reply here and I'll do it with you.
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
            f"Modules: {'Labor Optimizer, ' if has_labor else ''}{'Food Cost Control' if has_inventory else ''}\n"
            "Write the one-week check-in paragraph referencing this activity naturally."
        )
        body_paragraph = generate_email_personalization(ai_context, fallback_paragraph, restaurant_id=restaurant_id)

        deliver(email_type="send_onboarding_day7", restaurant_id=restaurant_id, payload={
            "from": f"Will Cavnar <{_from_email()}>",
            "to": [to_email],
            "subject": f"One week in — how's the dashboard feeling?",
            "html": _html_document(f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:560px;margin:0 auto;color:#1a1714;background:#f7f4ef;border-radius:12px;padding:32px 24px;box-sizing:border-box">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="170" height="30" alt="Cavnar AI" style="display:block;width:170px;height:30px;border:0;outline:none;margin:0 0 6px">
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
    <img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:6px;border:0"><span style="vertical-align:middle">Will Cavnar &nbsp;·&nbsp; Cavnar AI</span><br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>
</div>""")
        })
        print(f"Onboarding day 7 sent to {to_email}")
    except Exception as e:
        print(f"send_onboarding_day7 failed: {e}")


def send_reactivation_email(to_email: str, restaurant_name: str, owner_name: str = None,
                             db_path: str = None):
    """Send a welcome-back email when a client is reactivated."""
    if not _resend_key():
        return
    try:
        first = owner_name.split()[0] if owner_name else "there"
        deliver(email_type="send_reactivation_email", payload={
            "from": f"Will Cavnar <{_from_email()}>",
            "to": [to_email],
            "subject": f"Welcome back to Cavnar AI — {restaurant_name}",
            "html": _html_document(f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:560px;margin:0 auto;color:#1a1714;background:#f7f4ef;border-radius:12px;padding:32px 24px;box-sizing:border-box">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="170" height="30" alt="Cavnar AI" style="display:block;width:170px;height:30px;border:0;outline:none;margin:0 0 6px">
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
  <a href="https://dashboard.cavnar.ai" style="display:inline-block;background:#c84b2f;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif">Go to dashboard →</a>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Questions? Reply to this email or reach me at
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    · <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>
</div>""")
        })
    except Exception as e:
        print(f"send_reactivation_email failed: {e}")


def send_monthly_summary_email(to_email: str, restaurant_name: str, owner_name: str = None,
                                restaurant_id: int = None,
                                has_reviews: bool = True, has_labor: bool = False,
                                has_inventory: bool = False, has_marketing: bool = False):
    """The month in review, on the shared report layout (see report_shell).

    What this used to be: a flex stat row that collapsed in Outlook, then one
    generic paragraph per module — "Your labor data has been analyzed all
    month. Log in to see your latest cost breakdown." — which was written
    once and sent to everyone, true or not. It now reads this restaurant's
    own rows and says one short, checkable thing per module, or nothing.
    """
    if not _resend_key():
        return
    try:
        from datetime import datetime, timedelta
        first = owner_name.split()[0] if owner_name else "there"
        now = datetime.now()
        last_day = now.replace(day=1) - timedelta(days=1)
        month_name, year = last_day.strftime("%B"), last_day.year

        usage = restaurant_usage(restaurant_id)
        owns, used = usage.get("owns") or {}, usage.get("used") or {}

        def owned(key, fallback):
            return bool(owns[key]) if key in owns else bool(fallback)

        r_reviews = owned("reviews", has_reviews)
        r_labor = owned("labor", has_labor)
        r_inventory = owned("inventory", has_inventory)
        r_marketing = owned("marketing", has_marketing)

        # ── Reviews, from this restaurant's own reviews table ──────────────
        total = pos = neg = 0
        avg = 0.0
        if r_reviews and restaurant_id:
            try:
                from models import get_reviews_since
                month_start = now.replace(day=1, hour=0, minute=0, second=0) - timedelta(days=30)
                reviews = get_reviews_since(restaurant_id, month_start.isoformat())
                total = len(reviews)
                if total:
                    avg = round(sum(r.rating for r in reviews) / total, 1)
                    pos = sum(1 for r in reviews if r.rating >= 4)
                    neg = sum(1 for r in reviews if r.rating <= 2)
            except Exception:
                pass

        stats_section = ""
        if r_reviews and total:
            stats_section = (
                report_eyebrow("Reviews in " + month_name) +
                report_stats([
                    (total, "new reviews"),
                    (f"{avg}&#9733;", "avg rating",
                     BRAND["good"] if avg >= 4.2 else (BRAND["warn"] if avg >= 3.5 else BRAND["bad"])),
                    (pos, "positive", BRAND["good"] if pos else None),
                    (neg, "negative", BRAND["bad"] if neg else None),
                ])
            )

        # ── One checkable line per module they actually own ────────────────
        pending = usage.get("pending_reviews", 0)
        approved = usage.get("approved_reviews", 0)
        pos_name = usage.get("pos")
        ingredients = usage.get("ingredient_count", 0)

        lines = []
        if r_reviews:
            if approved:
                txt = f"{approved} response{'' if approved == 1 else 's'} approved and posted to date."
            elif total:
                txt = "Draft responses are written and waiting on your approval."
            else:
                txt = f"No new reviews came in during {month_name}."
            if pending:
                txt += f" {pending} repl{'y is' if pending == 1 else 'ies are'} still waiting on you."
            lines.append(("Review Intelligence", txt))
        if r_labor:
            if usage.get("has_schedule"):
                lines.append(("Labor Optimizer",
                              "Schedules are building"
                              + (f" from your {pos_name} data." if pos_name else " from your shift history.")))
            elif pos_name:
                lines.append(("Labor Optimizer",
                              f"{pos_name} is connected — no schedule has been generated yet."))
            else:
                lines.append(("Labor Optimizer",
                              "No POS connected yet, so schedules are still being built by hand."))
        if r_inventory:
            lines.append(("Food Cost Control",
                          f"{ingredients} ingredient{'' if ingredients == 1 else 's'} tracked."
                          if ingredients else "No ingredients added yet, so waste isn't being tracked."))
        if r_marketing:
            lines.append(("Marketing Autopilot",
                          "Content is generating from your profile."
                          if used.get("marketing") else "No content generated yet this month."))
        lines_section = report_lines(lines)

        # ── One action, chosen from what's genuinely outstanding ───────────
        action = None
        if r_reviews and pending:
            action = (f"Approve the {pending} drafted repl{'y' if pending == 1 else 'ies'} "
                      "sitting in Reviews — it takes about five minutes.")
        elif r_labor and not usage.get("has_schedule"):
            action = ("Generate next month's opening schedule in Labor"
                      + (f" — your {pos_name} history is already there." if pos_name
                         else " once your POS is connected under Account → Connections."))
        elif r_inventory and not ingredients:
            action = ("Add the fifteen or twenty items you buy most weeks in Food Cost. "
                      "That's enough for it to start flagging waste.")
        elif r_marketing and not used.get("marketing"):
            action = "Generate one post in Marketing — the first one takes about thirty seconds to approve."
        action_section = report_action("Your next move", action) if action else ""

        # ── Opening line, personalised on the real numbers above ───────────
        fallback_paragraph = (
            f"Here's how {restaurant_name} did on Cavnar AI in {month_name}."
            if not total else
            f"{restaurant_name} picked up {total} new review{'' if total == 1 else 's'} in "
            f"{month_name} at a {avg}★ average."
        )
        modules_in_use = ", ".join(m for m, on in [
            ("Review Intelligence", r_reviews), ("Labor Optimizer", r_labor),
            ("Food Cost Control", r_inventory), ("Marketing Autopilot", r_marketing),
        ] if on) or "Review Intelligence"
        ai_context = (
            f"Restaurant: {restaurant_name}. This is their {month_name} {year} monthly summary email.\n"
            f"Modules in use: {modules_in_use}.\n"
            + (f"Reviews this month: {total} total, {avg} star average, {pos} positive, {neg} negative.\n"
               if total else "No new reviews this month.\n")
            + "Write the opening line. The stat row directly beneath it already shows the "
              "totals, so do not recite them all — lead with the one thing that matters most."
        )
        summary_paragraph = generate_email_personalization(
            ai_context, fallback_paragraph, restaurant_id=restaurant_id, brief=True)

        deliver(email_type="send_monthly_summary_email", restaurant_id=restaurant_id, payload={
            "from": f"Will Cavnar <{_from_email()}>",
            "to": [to_email],
            "subject": f"{month_name} at {restaurant_name} — your Cavnar AI summary",
            "html": report_shell(
                kicker="Monthly Summary",
                title=restaurant_name,
                subtitle=f"{month_name} {year} &nbsp;&middot;&nbsp; for {first}",
                sections=[report_paragraph(summary_paragraph), stats_section,
                          lines_section, action_section],
                cta_label="Open your dashboard →",
            ),
        })
    except Exception as e:
        print(f"send_monthly_summary_email failed: {e}")


def send_onboarding_day30(to_email: str, restaurant_name: str, owner_name: str = None,
                           modules: list = None, restaurant_id: int = None):
    """Day 30 — 30-day check-in, celebrate milestone, soft feedback ask."""
    if not _resend_key():
        return
    try:
        first = owner_name.split()[0] if owner_name else "there"
        modules = _module_display_names(modules)

        # Two genuinely different things, which this used to conflate into
        # one false sentence. It computed "you're not currently using X" as
        # every module MINUS the ones they bought — so it named modules the
        # client had never purchased and told them they weren't using them.
        #
        #   idle    = modules they OWN and have no activity in. That is the
        #             only thing "not using" can honestly mean, and it's
        #             worth a nudge.
        #   missing = modules they don't have. A real upsell, phrased as one.
        usage = restaurant_usage(restaurant_id)
        owns, used = usage.get("owns") or {}, usage.get("used") or {}
        idle = [_MODULE_DISPLAY_NAMES[k] for k in ("reviews", "labor", "inventory", "marketing")
                if owns.get(k) and not used.get(k)]
        missing = [_MODULE_DISPLAY_NAMES[k] for k in ("reviews", "labor", "inventory", "marketing")
                   if owns and not owns.get(k)]

        def _join(items):
            return " and ".join(items) if len(items) <= 2 else ", ".join(items[:-1]) + f", and {items[-1]}"

        upsell_block = ""
        if idle:
            upsell_block += f"""
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:20px">
    One thing I noticed: you haven't used <strong>{_join(idle)}</strong> yet — it's set up and
    included in what you're already paying for. Reply here and I'll get you going in ten minutes.
  </p>"""
        if missing:
            upsell_block += f"""
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:20px">
    If you ever want the dashboard to cover more, <strong>{_join(missing)}</strong> {"is" if len(missing) == 1 else "are"}
    what you don't have yet. Just reply and I'll walk you through what's included.
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
        body_paragraph = generate_email_personalization(ai_context, fallback_paragraph, restaurant_id=restaurant_id)

        deliver(email_type="send_onboarding_day30", restaurant_id=restaurant_id, payload={
            "from": f"Will Cavnar <{_from_email()}>",
            "to": [to_email],
            "subject": f"30 days of Cavnar AI — a quick check-in",
            "html": _html_document(f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:560px;margin:0 auto;color:#1a1714;background:#f7f4ef;border-radius:12px;padding:32px 24px;box-sizing:border-box">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="170" height="30" alt="Cavnar AI" style="display:block;width:170px;height:30px;border:0;outline:none;margin:0 0 6px">
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
    <img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:6px;border:0"><span style="vertical-align:middle">Will Cavnar &nbsp;·&nbsp; Cavnar AI</span><br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://calendly.com/will-cavnar/30min" style="color:#c84b2f;text-decoration:none">Book a call</a>
  </p>
</div>
</div>""")
        })
        print(f"Onboarding day 30 sent to {to_email}")
    except Exception as e:
        print(f"send_onboarding_day30 failed: {e}")


# ── Account-security confirmations ─────────────────────────────────────────
# Three real gaps found in a full email-system audit: a card decline only
# ever reached Will (the client had no idea their own card failed), and
# neither a self-service password change nor an email change sent any
# confirmation at all — a genuine security gap, since someone changing
# either from inside an already-compromised account would do so silently.

def send_password_changed_email(to_email: str, restaurant_name: str, owner_name: str = None):
    """Confirms a password change back to the account — same security-
    notification family as send_login_notification, deliberately (this is
    exactly as sensitive an event)."""
    if not _resend_key():
        log.warning("send_password_changed_email: RESEND_API_KEY not set — nothing sent")
        return False
    import requests
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now_str = datetime.now(ZoneInfo("America/Chicago")).strftime("%b %d, %Y at %I:%M %p CT")
    except Exception:
        now_str = datetime.utcnow().strftime("%b %d, %Y at %H:%M UTC")
    html = f"""
    <div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:480px;margin:0 auto;background:#f7f4ef;padding:32px 24px;border-radius:12px">
      <div style="text-align:center;margin-bottom:24px">
        <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="180" height="32" alt="Cavnar AI" style="display:inline-block;width:180px;height:32px;border:0;outline:none">
      </div>
      <div style="background:white;border-radius:10px;padding:28px 24px;border:1px solid #e0dbd0">
        <p style="color:#3a3530;font-size:15px;margin:0 0 16px">Your password for <strong>{restaurant_name}</strong> was changed on {now_str}.</p>
        <p style="color:#7a736a;font-size:13px;margin:20px 0 0;line-height:1.6">If this was you, no action needed. If you didn't make this change, someone else may have access to your account — <a href="mailto:will@cavnar.ai" style="color:#c84b2f">contact Will immediately</a>.</p>
      </div>
      <p style="color:#7a736a;font-size:11px;text-align:center;margin-top:20px"><img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:5px;border:0">Cavnar AI &mdash; Restaurant Intelligence Platform</p>
    </div>
    </div>
    """
    try:
        _res = deliver(email_type="send_password_changed_email", payload={"from": f"Cavnar AI <{_from_email()}>", "to": [to_email],
                  "subject": "Your Cavnar AI password was changed", "html": _html_document(html)})
        return _res
    except Exception as e:
        log.warning("send_password_changed_email: request to Resend failed: %s", e)
        return False


def send_email_changed_email(to_email: str, restaurant_name: str, new_email: str, owner_name: str = None):
    """Sent to the OLD address when the account email changes — the
    security-critical direction (the new address already knows, since they
    just typed it in; the old address is where an actual account takeover
    would otherwise go unnoticed)."""
    if not _resend_key():
        log.warning("send_email_changed_email: RESEND_API_KEY not set — nothing sent")
        return False
    import requests
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now_str = datetime.now(ZoneInfo("America/Chicago")).strftime("%b %d, %Y at %I:%M %p CT")
    except Exception:
        now_str = datetime.utcnow().strftime("%b %d, %Y at %H:%M UTC")
    masked_new = new_email[:2] + "***@" + new_email.split("@")[-1]
    html = f"""
    <div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:480px;margin:0 auto;background:#f7f4ef;padding:32px 24px;border-radius:12px">
      <div style="text-align:center;margin-bottom:24px">
        <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="180" height="32" alt="Cavnar AI" style="display:inline-block;width:180px;height:32px;border:0;outline:none">
      </div>
      <div style="background:white;border-radius:10px;padding:28px 24px;border:1px solid #e0dbd0">
        <p style="color:#3a3530;font-size:15px;margin:0 0 16px">The sign-in email for <strong>{restaurant_name}</strong> was changed on {now_str}, from this address to <strong>{masked_new}</strong>.</p>
        <p style="color:#7a736a;font-size:13px;margin:20px 0 0;line-height:1.6">If this was you, no action needed — this is the last email you'll receive at this address. If you didn't make this change, <a href="mailto:will@cavnar.ai" style="color:#c84b2f">contact Will immediately</a>, since someone else may now control sign-in to this account.</p>
      </div>
      <p style="color:#7a736a;font-size:11px;text-align:center;margin-top:20px"><img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:5px;border:0">Cavnar AI &mdash; Restaurant Intelligence Platform</p>
    </div>
    </div>
    """
    try:
        _res = deliver(email_type="send_email_changed_email", payload={"from": f"Cavnar AI <{_from_email()}>", "to": [to_email],
                  "subject": "Your Cavnar AI sign-in email was changed", "html": _html_document(html)})
        return _res
    except Exception as e:
        log.warning("send_email_changed_email: request to Resend failed: %s", e)
        return False


def send_payment_failed_client_email(to_email: str, restaurant_name: str, amount: float, owner_name: str = None):
    """The client-facing half of a failed card charge — webhook_routes.py's
    stripe_webhook() already alerts Will on invoice.payment_failed, but the
    client themselves never found out except by Will personally reaching
    out. This is what actually gets a card fixed quickly."""
    if not _resend_key():
        log.warning("send_payment_failed_client_email: RESEND_API_KEY not set — nothing sent")
        return False
    import requests
    greeting = f"Hi {owner_name}," if owner_name else "Hi,"
    html = f"""
    <div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:480px;margin:0 auto;background:#f7f4ef;padding:32px 24px;border-radius:12px">
      <div style="text-align:center;margin-bottom:24px">
        <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="180" height="32" alt="Cavnar AI" style="display:inline-block;width:180px;height:32px;border:0;outline:none">
      </div>
      <div style="background:white;border-radius:10px;padding:28px 24px;border:1px solid #e0dbd0">
        <p style="color:#3a3530;font-size:15px;margin:0 0 16px">{greeting}</p>
        <p style="color:#3a3530;font-size:15px;margin:0 0 20px">Your payment of <strong>${amount:.2f}</strong> for <strong>{restaurant_name}</strong> didn't go through — your card was declined.</p>
        <a href="https://dashboard.cavnar.ai" style="display:inline-block;background:#c84b2f;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">Update payment method &#8594;</a>
        <p style="color:#7a736a;font-size:13px;margin:20px 0 0;line-height:1.6">Open the Cavnar AI app and go to <strong>Account &rarr; Billing</strong> — the Manage billing button there opens the secure Stripe page where you can update your card. If it isn't resolved in a few days, reach out and I'll sort it out with you — <a href="mailto:will@cavnar.ai" style="color:#c84b2f">will@cavnar.ai</a>.</p>
      </div>
      <p style="color:#7a736a;font-size:11px;text-align:center;margin-top:20px"><img src="https://dashboard.cavnar.ai/static/brand/seal-dark-email.png" width="14" height="14" alt="" style="vertical-align:middle;margin-right:5px;border:0">Cavnar AI &mdash; Restaurant Intelligence Platform</p>
    </div>
    </div>
    """
    try:
        _res = deliver(email_type="send_payment_failed_client_email", payload={"from": f"Will Cavnar <{_from_email()}>", "to": [to_email],
                  "subject": f"Payment issue — {restaurant_name}", "html": _html_document(html)})
        return _res
    except Exception as e:
        log.warning("send_payment_failed_client_email: request to Resend failed: %s", e)
        return False


def send_recovery_email_code(to_email: str, code: str) -> bool:
    """Verifies a recovery address before it counts — a typo here would
    otherwise be the address that can reset the password."""
    return _send_branded(to_email, "Confirm your Cavnar AI recovery email", f"""
      <h2 style="font-size:18px;font-weight:600;margin-bottom:12px;color:#0e0c0a">Confirm this recovery email</h2>
      <p style="font-size:14px;color:#4a4540;line-height:1.6;margin-bottom:20px">Enter this code in the app to finish adding this address as your recovery email. It expires in 15 minutes.</p>
      <p style="font-size:32px;font-weight:700;letter-spacing:6px;color:#c84b2f;margin:0 0 20px">{code}</p>
      <p style="font-size:12px;color:#7a736a;line-height:1.6">If you didn't request this, you can ignore it.</p>
    """)


def send_account_deletion_request_email(restaurant_name: str, owner_name: str, owner_email: str, requested_at: str) -> bool:
    """Fires the moment Account -> Close my account is tapped. Cavnar AI
    can't self-serve deactivate an account under contract, so this is the
    actual initiation Apple's account-deletion requirement asks for: it
    lands in Will's inbox so he can start the 30-day wind-down, the same
    manual process as before — the difference is the request now comes
    from a real in-app action instead of the owner having to know to email
    him themselves."""
    return _send_branded(os.getenv("BUG_REPORT_EMAIL", "will@cavnar.ai"),
        f"Account deletion requested — {restaurant_name}", f"""
      <h2 style="font-size:18px;font-weight:600;margin-bottom:12px;color:#0e0c0a">{restaurant_name} requested account deletion</h2>
      <p style="font-size:14px;color:#4a4540;line-height:1.6;margin-bottom:16px">
        {owner_name or "The owner"} ({owner_email or "no email on file"}) tapped
        "Close my account" in the app at {requested_at} UTC. Per the 30-day
        notice policy, the account stays active through the end of the
        current billing period plus 30 days from this request — reach out
        to confirm and start winding it down.
      </p>
    """, from_label="Cavnar AI Account Requests")


def send_bug_report_email(restaurant_name: str, from_email: str, message: str, meta: dict) -> bool:
    """Account -> More -> Report a bug. Lands in Will's inbox with the build
    stamp and device details attached, so 'which build is this' never has
    to be asked."""
    rows = "".join(f"<tr><td style='padding:4px 12px 4px 0;color:#7a736a'>{k}</td><td style='padding:4px 0'><strong>{v}</strong></td></tr>"
                   for k, v in (meta or {}).items() if v)
    safe = (message or "").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    return _send_branded(os.getenv("BUG_REPORT_EMAIL", "will@cavnar.ai"), f"Bug report — {restaurant_name}", f"""
      <h2 style="font-size:18px;font-weight:600;margin-bottom:12px;color:#0e0c0a">Bug report from {restaurant_name}</h2>
      <p style="font-size:14px;color:#4a4540;line-height:1.6;margin-bottom:16px">From {from_email}</p>
      <div style="font-size:14px;color:#0e0c0a;line-height:1.6;background:#f7f4ef;padding:14px;border-radius:8px;margin-bottom:16px">{safe}</div>
      <table style="font-size:13px;border-collapse:collapse">{rows}</table>
    """, from_label="Cavnar AI Bug Reports")
