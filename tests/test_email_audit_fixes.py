"""Email audit (Sep 19 2026) — the findings that survived verification.

Four of the original findings did not, and are deliberately NOT tested here
because they were never true: the digest's AI summary is figure-guarded
(unsupported_figures), it does carry cross-module correlations, it does
state dollars, and the daily backup email carries the encrypted database as
an attachment rather than being a status ping.
"""
import pytest

import emails
import models
import scheduler


@pytest.fixture
def rid(db_path):
    return models.create_restaurant(
        models.Restaurant(name="Simple EJ's", owner_email="erik@x.test"), db_path=db_path)


# ── sender identity ─────────────────────────────────────────────────────────

def test_one_sender_identity_per_audience(monkeypatch):
    """Ten display names on one address is ten weak reputation signals and a
    sender an owner cannot learn to recognise."""
    monkeypatch.setattr(emails, "_from_email", lambda: "will@cavnar.ai")
    assert emails.sender("client") == "Cavnar AI <will@cavnar.ai>"
    assert emails.sender("ops") == "Cavnar AI Ops <will@cavnar.ai>"
    assert emails.sender("will") == "Will Cavnar <will@cavnar.ai>"
    # An unknown audience is a client, never an ops leak.
    assert emails.sender("nonsense") == "Cavnar AI <will@cavnar.ai>"


def test_no_alerts_or_backups_sender_survives():
    """The variants that fragmented the sender: Alerts, Ops-on-client-mail,
    Backups, Labor Alerts."""
    import notify
    src = open("notify.py").read() + open("emails.py").read()
    for gone in ("Cavnar AI Alerts <", "Cavnar AI Backups <", "Cavnar AI Labor Alerts <"):
        assert gone not in src, f"{gone} is still a sender"


# ── preheader ───────────────────────────────────────────────────────────────

def test_preheader_is_injected_and_hidden():
    html = "<html><body><h1>Your week</h1></body></html>"
    out = emails.with_preheader(html, "Food cost ran 34.2%, 4 points over.")
    assert "Food cost ran 34.2%" in out
    assert "display:none" in out
    # Must sit BEFORE the visible content or the client previews the h1.
    assert out.index("Food cost ran") < out.index("Your week")


def test_preheader_is_padded_so_body_text_does_not_bleed_in():
    out = emails.with_preheader("<html><body>x</body></html>", "Lead line.")
    assert out.count("&zwnj;") > 10


def test_no_preheader_is_a_no_op():
    html = "<html><body>x</body></html>"
    assert emails.with_preheader(html, "") == html


def test_deliver_strips_preheader_before_resend_sees_it(monkeypatch):
    """Resend has no such field; it must become HTML, not a rejected key."""
    captured = {}

    class _Resp:
        status_code = 200
        text = '{"id":"m1"}'
        @staticmethod
        def json(): return {"id": "m1"}

    monkeypatch.setattr(emails, "_resend_key", lambda: "re_fake")
    monkeypatch.setattr("requests.post",
                        lambda url, **k: captured.update(k.get("json") or {}) or _Resp())
    emails.deliver(email_type="digest", payload={
        "from": "Cavnar AI <will@cavnar.ai>", "to": ["erik@x.test"],
        "subject": "s", "preheader": "the lead line", "html": "<html><body>b</body></html>"})

    assert "preheader" not in captured
    assert "the lead line" in captured["html"]


# ── greeting ────────────────────────────────────────────────────────────────

def test_greeting_never_mangles_a_mailbox_name():
    """An owner with no sign_off_name was greeted "Cavnarwill" — the first
    word of the most-sent email in the product."""
    class _R:
        sign_off_name = None
        owner_name = None
        owner_email = "cavnarwill@gmail.com"
    assert emails.greeting_name(_R()) is None


def test_greeting_prefers_the_name_the_owner_chose():
    class _R:
        sign_off_name = "Erik Sandoval"
        owner_name = "Somebody Else"
    assert emails.greeting_name(_R()) == "Erik"


# ── cost of waiting ─────────────────────────────────────────────────────────

def _review(**metric):
    base = {"label": "Food cost %", "verdict": "worsened", "monthly_dollars": 1450.0}
    base.update(metric)
    return {"metrics": [base]}


def test_cost_of_waiting_states_the_month_and_the_year():
    import weekly_review
    out = weekly_review.cost_of_waiting(_review())
    assert "$1,450" in out and "$17,400" in out


def test_cost_of_waiting_is_silent_when_nothing_worsened():
    import weekly_review, monthly_review
    for mod in (weekly_review, monthly_review):
        assert mod.cost_of_waiting(_review(verdict="improved")) == ""
        assert mod.cost_of_waiting(_review(verdict="no_clear_change")) == ""


def test_cost_of_waiting_needs_a_dollar_figure():
    """A warning without a number is the generic nudge this replaces."""
    import monthly_review
    assert monthly_review.cost_of_waiting(_review(monthly_dollars=None)) == ""


def test_cost_of_waiting_leads_with_the_most_expensive():
    import monthly_review
    review = {"metrics": [
        {"label": "Rating", "verdict": "worsened", "monthly_dollars": 200.0},
        {"label": "Labor %", "verdict": "worsened", "monthly_dollars": 2100.0},
    ]}
    assert "labor %" in monthly_review.cost_of_waiting(review).lower()


# ── subjects ────────────────────────────────────────────────────────────────

def test_client_subjects_carry_no_emoji():
    """On a locked phone an emoji-led subject reads as a consumer app, not a
    CFO. Internal triage mail to Will keeps its glyphs."""
    def has_emoji(text):
        return any(
            0x1F300 <= ord(c) <= 0x1FAFF      # pictographs
            or 0x2600 <= ord(c) <= 0x27BF     # misc symbols, dingbats
            or ord(c) == 0x2B50               # star
            for c in text)

    src = open("notify.py").read()
    offenders = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if ("blast(" in line or "raise_alert(" in line or 'subject = f"' in line) \
                and has_emoji(line):
            offenders.append(stripped)
    assert not offenders, f"emoji in client subjects: {offenders[:3]}"


# ── the design system now covers email ──────────────────────────────────────

def test_email_token_lint_runs_and_holds():
    """A ratchet is only a ratchet if something runs it."""
    import subprocess, sys
    out = subprocess.run([sys.executable, "scripts/check_email_tokens.py"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_design_system_governs_email():
    """22 client emails across three frames happened because the document
    written to stop drift did not mention the surface clients see most."""
    doc = open("DESIGN_SYSTEM.md").read()
    assert "\n## Email\n" in doc
    for rule in ("report_shell", "preheader", "Light mode only", "BRAND"):
        assert rule in doc, f"DESIGN_SYSTEM.md → Email says nothing about {rule}"


# ── engagement ──────────────────────────────────────────────────────────────

def test_an_open_does_not_erase_the_delivery_state(db_path):
    """status is the DELIVERY state. Writing "opened" into it would lose the
    fact that it was delivered, and the two answer different questions."""
    models.init_email_log(db_path=db_path)
    models.log_email(1, "digest", "erik@x.test", "Your week", db_path=db_path,
                     message_id="m-1", status="delivered")
    assert models.mark_email_engagement("m-1", "opened", db_path=db_path) is True

    conn = models.get_conn(db_path)
    row = conn.execute("SELECT status, opened_at, clicked_at FROM email_log "
                       "WHERE message_id='m-1'").fetchone()
    conn.close()
    assert row["status"] == "delivered"
    assert row["opened_at"] and row["clicked_at"] is None


def test_only_the_first_open_is_recorded(db_path):
    """A client that re-fetches images on every scroll would otherwise
    rewrite the timestamp all day."""
    models.init_email_log(db_path=db_path)
    models.log_email(1, "digest", "erik@x.test", "s", db_path=db_path, message_id="m-2")
    assert models.mark_email_engagement("m-2", "opened", db_path=db_path) is True
    assert models.mark_email_engagement("m-2", "opened", db_path=db_path) is False


def test_engagement_rate_is_per_type(db_path):
    models.init_email_log(db_path=db_path)
    for i in range(4):
        models.log_email(1, "digest", "erik@x.test", "s", db_path=db_path,
                         message_id=f"d{i}")
    models.mark_email_engagement("d0", "opened", db_path=db_path)
    models.log_email(1, "send_2fa_code", "erik@x.test", "s", db_path=db_path, message_id="t0")

    rows = {r["email_type"]: r for r in models.email_engagement(1, db_path=db_path)}
    assert rows["digest"]["sent"] == 4 and rows["digest"]["opened"] == 1
    assert rows["digest"]["open_rate"] == 25.0
    assert rows["send_2fa_code"]["opened"] == 0


def test_a_bad_engagement_kind_is_refused(db_path):
    """The column name is interpolated into SQL — it may only ever come from
    the fixed map, never from the webhook payload."""
    models.init_email_log(db_path=db_path)
    models.log_email(1, "digest", "e@x.test", "s", db_path=db_path, message_id="m-3")
    assert models.mark_email_engagement("m-3", "status='x'--", db_path=db_path) is False


# ── onboarding ──────────────────────────────────────────────────────────────

def test_day_seven_is_skipped_for_an_owner_already_using_it(db_path, monkeypatch):
    """"Here's what you're missing" to someone who signs in every morning is
    the clearest possible sign nobody reads what they send."""
    import datetime as _dt
    from auth import create_user, init_auth
    init_auth(db_path=db_path)
    rid = models.create_restaurant(
        models.Restaurant(name="Busy Co", owner_email="o@x.test"), db_path=db_path)
    uid = create_user(rid, "owner", "o@x.test", "correct-horse", db_path=db_path)

    real = models.get_conn
    monkeypatch.setattr(scheduler, "log", scheduler.log)
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))

    conn = real(db_path)
    conn.execute("UPDATE users SET last_login=? WHERE id=?",
                 (_dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), uid))
    conn.commit(); conn.close()

    logins, days_idle = scheduler._onboarding_engagement(rid)
    assert logins >= 1 and days_idle == 0


def test_engagement_lookup_fails_toward_sending(monkeypatch):
    """A settled client getting one extra tip email is a smaller failure
    than a new client getting no onboarding at all."""
    monkeypatch.setattr(models, "get_conn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert scheduler._onboarding_engagement(1) == (0, None)
