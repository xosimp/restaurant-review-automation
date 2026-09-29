"""Fix round, UI wave 1 — the admin console's Overview, Customers, the client
page, the rail, the palette and the modals (templates/admin.html).

Two halves:

- Source rules, read from the template (the suite has no JS engine): a
  failed read is never drawn as zero or "all clear"; Resolve carries the
  occurrence it saw; every write control is marked `.w` (hidden from a
  support login); the admin never sets or sees a client's password; the
  rail polls the slim badges read; the New client timezones are the
  product's own list.
- A render check: the Flask app built as the admin tests build it, signed in
  as an admin, GETs /admin and every JSON read the console makes, and
  asserts each answers 200 with the fields the page reads — so a renamed
  field fails here, not on Will's screen.
"""
import os
import re
import sys

import pytest
from flask import Flask

import admin_ops
import admin_routes
import auth
import models
import time_utils
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def page():
    with open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8") as f:
        return f.read()


def _between(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i + len(start))]


def _fn(src, name):
    """One function's source, from its declaration to the next top-level one."""
    m = re.search(r"\n(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"{name}() is gone from admin.html"
    nxt = re.search(r"\n(?:async\s+)?function\s+\w+\s*\(|\n// ──", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src))]


# The regions this wave owns (UI-1): the modal HTML, the rail, the palette,
# Overview, the shared pieces, Customers through the client page, and the
# action helpers at the end.
def _mine(src):
    return "\n".join([
        _between(src, "<!-- modals -->", '<div class="mo" id="m-incident">'),
        _between(src, "// ── router", "// ── Operations"),
        _between(src, "// ── Client detail", "\n// go\n"),
        _fn(src, "createClient"), _fn(src, "seedReviewAccount"),
    ])


# ── a failed read is unknown, never zero or "all clear" (#47, #128) ─────────

def test_every_fleet_view_carries_the_failed_read_banner_and_its_age(page):
    for name in ("overview", "clientsView", "onboarding", "billing", "issues", "clientView", "locations"):
        body = _fn(page, name)
        assert "errBanner(d" in body, f"{name}() draws no failed-read banner"
    for name in ("overview", "billing", "issues", "clientView"):
        assert "asOf(d)" in _fn(page, name), f"{name}() doesn't say how old its figures are"
    # The client page's own `errors` is its failure list, not the reads that failed.
    assert "errBanner(d, true)" in _fn(page, "clientView")


def test_overview_figures_built_on_a_failed_read_say_unknown(page):
    ov = _fn(page, "overview")
    # Each figure is tied to the tables its value came from.
    for tables in ("['job_failures']", "['ai_usage']", "['email_log']", "['restaurants', 'stripe_subscriptions']"):
        assert tables in ov, tables
    assert "figOr(" in ov and "unknownV(fleetErr)" in ov
    helper = _fn(page, "figOr")
    assert "unknownV(" in helper
    # "Nothing needs you" is said only when the fleet was actually read.
    needs = _fn(page, "needsList")
    i_fail, i_clear = needs.index("readFailed(d, ['restaurants'])"), needs.index("Nothing needs you")
    assert i_fail < i_clear


def test_a_system_whose_read_failed_is_grey_never_green(page):
    ps = _fn(page, "platformSystems")
    assert "'unk'" in ps and "readFailed(d" in ps
    ring = _fn(page, "healthRing")
    assert "s[1]==='ok'" in ring            # only an ok reading counts as healthy
    assert "unk:'rgba(255,255,255,.2)'" in page.replace(" ", "")  or "unk: 'rgba(255,255,255,.2)'" in page
    # SMS is the seventh system (#14), read from messaging health.
    assert "['SMS'," in ps and "/admin/api/messaging/health" in _fn(page, "overview")


def test_the_rail_polls_the_slim_badges_and_says_unknown_when_it_cannot(page):
    b = _fn(page, "badges")
    assert "/admin/api/badges" in b and "/admin/api/overview" not in b
    assert "document.hidden" in b and "railStale(" in b
    assert "setInterval(badges, 120000)" in page
    stale = _fn(page, "railStale")
    assert "classList.add('stale')" in stale and "unknown" in stale
    ab = _fn(page, "applyBadges")
    # A heartbeat that couldn't be read is unknown; one that never beat is "never".
    assert "unknown" in ab and "'never'" in ab and "heartbeat" in ab
    assert "d.counts || d.issue_counts" in ab       # the badges payload or an overview payload
    assert ".rail.stale" in page


def test_bars_keep_empty_days_as_empty_slots(page):
    b = _fn(page, "bars")
    assert "'z'" in b and ": 2}px" in b
    assert ".bars i.z{" in page


def test_overview_series_come_from_the_server(page):
    ov = _fn(page, "overview")
    assert "d.series" in ov
    # The browser back-cast from today's accounts is gone.
    assert "mrrSeries(" not in page
    assert "api('/admin/api/clients')" not in ov
    g = _fn(page, "growthChart")
    assert "s.accounts" in g and "s.signups" in g


# ── issues: occurrence-scoped Resolve, reopened, actions by kind ────────────

def test_resolve_sends_the_occurrence_it_saw_and_a_409_says_why(page):
    row = _fn(page, "needRow")
    assert "resolveOpen('${jsq(i.key)}','${jsq(i.title)}','${jsq(i.occurrence_at||'')}')" in row
    assert "i.resolvable === false" in row and "clears itself" in row
    assert "i.reopened" in row and "resolved it on" in row
    submit = _fn(page, "resolveSubmit")
    assert "occurrence_at:_resolve.occurrence_at" in submit
    assert "_status === 409" in submit and "resolvable === false" in submit


def test_an_issue_action_follows_its_kind(page):
    btn = _fn(page, "issueActBtn")
    assert "action_kind" in btn and "'post'" in btn and "mailto" in btn and "action_payload" in btn
    act = _fn(page, "issueAction")
    assert "JSON.parse(payload)" in act and "post(path, body)" in act and "say(d" in act


# ── every write control is a .w, and says what it does ──────────────────────

WRITE_FNS = ("act", "setDemo", "deleteDemo", "setRole", "sendResetLink", "freezeAccount", "resetPassword",
             "resendWelcome", "resendPayment", "resendContract", "cardLink", "changePlan", "markSigned",
             "attachCustomer", "liftHold", "testSend", "valueRecap", "toastSave", "toastSync", "toastDisconnect",
             "rpowerSave", "rpowerBind", "rpowerSync", "rpowerDisconnect", "brandSave", "importSubmit",
             "addLocationSubmit", "addLocationOpen", "newClientOpen", "createClient", "resolveOpen", "resolveSubmit",
             "unresolve", "capOpen", "capSet", "capSubmit", "issueAction", "reviewsFetching", "contactAdd",
             "contactDelete", "templateAdd", "templateDelete", "staffNoteAdd", "staffNoteDelete", "menuExtract",
             "reinstate", "testText", "stormLift", "retryAi", "deactivateLogin", "reactivateLogin", "reset2fa",
             "clearLockout", "revokeSession", "revokeAll", "offStep", "withdrawDeletion", "deleteRestaurant",
             "noteAdd", "auditLink", "bugStatus", "vendorCostOpen", "formSubmit")


def test_every_write_control_in_these_regions_is_marked_w(page):
    mine = _mine(page)
    bad = []
    pat = re.compile(r"<(button|select|label)\b([^<>]*?)on(?:click|change)=\"(?:event\.stopPropagation\(\);)?"
                     r"(?:menusClose\(\);)?(" + "|".join(WRITE_FNS) + r")\(")
    for m in pat.finditer(mine):
        attrs = m.group(2)
        cls = re.search(r'class="([^"]*)"', attrs)
        if not cls or not re.search(r"(^|\s)w(\s|$)", cls.group(1)):
            bad.append(mine[m.start():m.start() + 90])
    assert not bad, "write controls not marked .w:\n" + "\n".join(bad[:8])
    # The Actions menu's own buttons carry it through its helper.
    menu = _fn(page, "actionsMenu")
    assert "data-a class=\"w ${cls||''}\"" in menu


def test_act_reports_the_servers_answer_on_any_non_2xx(page):
    ok = _fn(page, "okRes")
    assert "d.ok !== false" in ok and "_status >= 400" in ok
    act = _fn(page, "act")
    assert "okRes(d)" in act and "errText(d)" in act
    assert "d.job_id" in act and "pollJob(jobUrl(d.job_id))" in act       # #153
    assert "d.errors" in act                                               # #78: a fetch's own errors


def test_the_old_actions_menu_copy_is_gone(page):
    assert "Nothing here changes billing or deletes a real restaurant" not in page
    assert "offboarding" in _fn(page, "actionsMenu")


# ── passwords, roles, welcome, test sends, demo (#86, #60, #22, #127, #118) ──

def test_the_console_never_sets_or_shows_a_clients_password(page):
    rp = _fn(page, "resetPassword")
    assert "prompt(" not in rp and "pw-out" not in rp and "password:" not in rp
    assert "/admin/reset-password-by-restaurant/" in rp
    assert "Set a temporary password" not in page
    # The Shown-once box is only for a value the server hands over once.
    assert "password_once" in _fn(page, "jobSaid")


def test_roles_are_labelled_as_the_owner_sees_them_and_confirmed(page):
    assert "const ROLE_LABEL = {client:'Co-owner', owner:'Owner', manager:'Manager', member:'Teammate'};" in page
    assert "GM (single location)" not in page
    sr = _fn(page, "setRole")
    assert "confirm(" in sr and "/admin/api/set-user-role" in sr


def test_resend_welcome_names_the_address_and_keeps_the_password(page):
    rw = _fn(page, "resendWelcome")
    # One welcome email now (INT-2): its link's lifetime is the model's, and
    # the confirm says it — not the one hour B2's separate email used.
    import models
    days = models.SET_PASSWORD_LINK_HOURS // 24
    assert "set-password link" in rw and f"for {days} days" in rw and "current password keeps working" in rw


def test_test_sends_name_the_recipient_and_can_go_to_me(page):
    ts = _fn(page, "testSend")
    assert "{to:'me'}" in ts and "['me','Me" in ts and "ownerEmail" in ts


def test_turning_demo_on_needs_the_typed_name(page):
    sd = _fn(page, "setDemo")
    assert "confirm_name" in sd and "prompt(" in sd
    menu = _fn(page, "actionsMenu")
    assert "c.is_demo?b('Seed sample reviews" in menu          # seeding only for a demo


def test_a_signed_contract_offers_an_amendment(page):
    rc = _fn(page, "resendContract")
    assert "d.signed && d._status === 409" in rc and "{amendment: true}" in rc


# ── New client, Add location, Brand, Import (#152, #42, #23, #149) ─────────

def test_new_client_timezones_are_the_products_own_list():
    src = open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8").read()
    m = re.search(r"const TZ_CHOICES = \[(.*?)\];", src)
    pairs = re.findall(r"\['([^']+)','([^']+)'\]", m.group(1))
    assert [z for z, _l in pairs] == list(time_utils.COMMON_TIMEZONES)
    assert dict(pairs) == admin_routes._TZ_LABELS


def test_new_client_sends_the_timezone_and_follows_the_contract_job(page):
    cc = _fn(page, "createClient")
    assert "timezone:v('r-tz')" in cc
    assert "/admin/api/create-client/" in cc and "pollJob(" in cc
    assert "docusign_error" in cc and "Billing tab" in cc
    assert "/admin/api/audits/" in cc and "/link" in cc               # #66


def test_add_location_posts_to_the_brand_route(page):
    al = _fn(page, "addLocationSubmit")
    assert "/admin/api/brand/add-location" in al and "from_restaurant_id" in al and "audit_id" in al


def test_the_brand_form_is_filled_sends_only_changes_and_recovers_from_a_stale_save(page):
    bo = _fn(page, "brandOpen")
    assert "/admin/api/brand/" in bo and "brandReset()" in bo
    bs = _fn(page, "brandSave")
    assert "expected_version" in bs and "clear" in bs and "_status === 409" in bs and "d.current" in bs
    assert "brandReset()" in _fn(page, "brandClose")
    assert "keep current" not in _between(page, '<div class="mo" id="m-brand">', '<div class="mo" id="m-import">')


def test_the_import_goes_to_the_clients_own_route_and_says_what_it_skipped(page):
    im = _fn(page, "importSubmit")
    assert "/admin/import-reviews/" in im and "/api/import-tripadvisor" not in im
    assert "already_had" in im and "skipped" in im


# ── the client page's newer tabs ───────────────────────────────────────────

def test_the_client_page_reads_what_the_backend_now_serves(page):
    wanted = {
        "cBillingMain": ("/admin/api/billing/", "open_invoice", "next_payment_attempt", "hold"),
        "billingLive": ("/billing/live",),
        "secLoad": ("/admin/api/users/", "/security", "is_view_as", "two_factor"),
        "reset2fa": ("scope === 'restaurant'", "everyone"),
        "offboardHtml": ("/offboarding", "ready_to_delete", "outstanding"),
        "deleteRestaurant": ("confirm_name", "/delete"),
        "notesHtml": ("/notes", "legacy_internal_notes"),
        "timelineHtml": ("/timeline", "types", "next_before"),
        "auditHtml": ("/audit", "next_before_id"),
        "salesAuditsHtml": ("/audits",),
        "aiClientHtml": ("/admin/api/ai/client/", "meter(", "stalled_total"),
        "meter": ("resets_at", "w.over", "w.warn", "w.pct"),
        "retryAi": ("/retry-ai",),
        "suppressionsHtml": ("/admin/api/suppressions?restaurant_id=",),
        "reinstate": ("/admin/api/suppressions/reinstate",),
        "smsHtml": ("/admin/api/sms?restaurant_id=",),
        "stormHtml": ("storm_caps",),
        "stormLift": ("/storm-cap/lift",),
        "briefHtml": ("/brief-deliveries",),
        "valueRecap": ("/value-recap",),
        "reviewsFetching": ("/reviews-fetching",),
        "contactsHtml": ("/admin/alert-contacts/",),
        "templatesHtml": ("/admin/api/templates/",),
        "staffNoteDelete": ("Undo",),
        "menuExtract": ("/admin/api/menu-extract/", "pollJob("),
    }
    for fn, needles in wanted.items():
        body = _fn(page, fn)
        for n in needles:
            assert n in body, f"{fn}() no longer reads {n}"


def test_the_support_queue_is_paged_and_filtered_on_the_server(page):
    iss = _fn(page, "issues")
    assert "/admin/api/issues/list?" in iss and "pager(" in iss
    assert "/admin/api/bug-reports" in _fn(page, "bugReports")
    assert "/admin/api/deletion-requests" in _fn(page, "deletionRequests")
    assert "/admin/api/issues/resolutions" in _fn(page, "resolvedIssues")
    cv = _fn(page, "clientsView")
    assert "/admin/api/clients/list?" in cv and "pager(" in cv


def test_the_palette_says_what_each_result_matched(page):
    pal = _fn(page, "palRender")
    assert "r.match" in pal and "r.m" in pal


# ── render check: /admin and every read the console makes ──────────────────

@pytest.fixture
def world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
            if getattr(mod, "DB_PATH", None) == models.DB_PATH and mod is not models:
                monkeypatch.setattr(mod, "DB_PATH", db_path)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(admin_ops, "get_conn", redirect, raising=False)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    models.init_staff_notes(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    import push, webhooks, guest_marketing, sales_audits
    push.init_push(db_path)
    webhooks.init_webhooks(db_path)
    guest_marketing.init_guest_marketing(db_path)
    sales_audits.init_sales_audits(db_path=db_path)
    admin_ops.invalidate_fleet_cache()

    home = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="will@ui1.test", billing_status="internal"),
                             db_path=db_path)
    admin = create_user(home, "will", "will@ui1.test", "Admin-pass-2026", is_admin=True, db_path=db_path)

    def client(name, email, username, **kw):
        rid = create_restaurant(Restaurant(name=name, owner_email=email, **kw), db_path=db_path)
        uid = None
        if username:
            uid = create_user(rid, username, email, "Owner-pass-2026", db_path=db_path)
            upsert_membership(uid, rid, "client", db_path=db_path)
        return rid, uid

    rid, owner = client("Test Grill", "owner@ui1.test", "grill", google_place_id="ChIJui1test", timezone="America/Denver")
    update_restaurant(rid, {"billing_status": "active", "module_reviews": 1, "module_labor": 1, "contract_status": "signed",
                            "stripe_customer_id": "cus_UI1TEST", "reviews_live": 1}, db_path=db_path)
    g1, _ = client("Group One", "group@ui1.test", "groupowner", location_group="Group", location_name="One")
    g2, _ = client("Group Two", "group@ui1.test", None, location_group="Group", location_name="Two")
    closing, _ = client("Closing Cafe", "close@ui1.test", "closer")
    update_restaurant(closing, {"billing_status": "active", "module_reviews": 1}, db_path=db_path)
    conn = real(db_path)
    try:
        conn.execute("UPDATE restaurants SET deletion_requested_at=datetime('now','-3 days') WHERE id=?", (closing,))
        conn.execute("INSERT INTO stripe_subscriptions (restaurant_id, subscription_id, status, interval, interval_count, "
                     "amount_cents, quantity, currency) VALUES (?, 'sub_UI1', 'active', 'month', 1, 49900, 1, 'usd')", (rid,))
        conn.execute("INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, status, error) "
                     "VALUES (?, 'weekly_digest', 'owner@ui1.test', 'Your week', datetime('now','-1 hour'), 'failed', 'bounced')",
                     (rid,))
        conn.execute("INSERT INTO support_notes (restaurant_id, author, body) VALUES (?, 'will', 'Called the owner.')", (rid,))
        conn.execute("INSERT INTO bug_reports (restaurant_id, username, email, message, source) "
                     "VALUES (?, 'grill', 'owner@ui1.test', 'Chart is blank.', 'ios')", (rid,))
        conn.execute("INSERT INTO sms_log (restaurant_id, use_case, to_last4, status, error_code, error) "
                     "VALUES (?, 'alert', '4242', 'failed', '30007', 'Carrier filtered')", (rid,))
        conn.commit()
    finally:
        conn.close()
    aid = sales_audits.create_audit(answers={"restaurant_name": "Test Grill"}, db_path=db_path)
    import promise
    promise.link(aid, rid, db_path=db_path)

    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.jinja_env.filters["format_num"] = lambda v: v
    app.jinja_env.filters["format_date"] = lambda v: v
    from auth_routes import auth_bp
    from sales_audit_routes import audit_bp
    # CSRF wired before the first registration, as the app and the other
    # sales-audit tests do: a blueprint registered once can't take a
    # before_request afterwards, and later files in this process wire it.
    if not getattr(audit_bp, "_csrf_wired", False):
        from csrf import csrf_protect
        csrf_protect(audit_bp)
        audit_bp._csrf_wired = True
    for bp in (admin_routes.admin_bp, auth_bp, audit_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    c.set_cookie("session_token", create_session(admin, db_path=db_path))
    yield {"c": c, "rid": rid, "owner": owner, "closing": closing, "db": db_path}
    admin_ops.invalidate_fleet_cache()


def test_the_console_page_renders(world):
    r = world["c"].get("/admin")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "async function badges()" in html and 'id="m-addloc"' in html and 'id="m-form"' in html
    assert "{{" not in html and "{%" not in html


def _has(obj, path):
    """A dotted path, where `[]` steps into a list's first item."""
    cur = obj
    for part in path.split("."):
        if part.endswith("[]"):
            cur = cur[part[:-2]]
            assert isinstance(cur, list) and cur, f"{path}: {part[:-2]} is an empty list here"
            cur = cur[0]
        else:
            assert isinstance(cur, dict) and part in cur, f"{path}: no '{part}'"
            cur = cur[part]
    return True


READS = [
    ("/admin/api/badges", ["counts.customer.total", "counts.platform.critical", "counts.internal", "counts.attention",
                           "counts.critical", "kpis.scheduler_heartbeat_minutes", "kpis.past_due",
                           "kpis.deletion_requests", "generated_at"]),
    ("/admin/api/overview", ["kpis." + k for k in (
        "mrr", "mrr_list", "mrr_billed", "mrr_committed", "mrr_past_due", "mrr_source", "mrr_fallback_accounts",
        "mrr_mismatches", "covered_locations", "paused", "internal_accounts", "trial_conversion", "ai_quality_24h",
        "request_errors_24h", "email_bounced_today", "email_complained_today", "stalled_reviews", "deletion_requests",
        "fetch_behind", "email_rates", "active", "clients", "trial", "past_due", "new_this_month", "critical",
        "scheduler_heartbeat_minutes", "job_failures_24h", "jobs_overdue", "ai_calls_today", "ai_cost_today",
        "ai_failures_24h", "emails_today", "email_failures_today", "push_failures_today")]
     + ["series.mrr", "series.signups[].week_start", "series.signups[].n", "series.accounts[].paying", "series.source",
        "series.note", "issue_counts.customer", "issue_counts.internal", "issues_total", "internal_issues",
        "issues[].occurrence_at", "issues[].resolvable", "issues[].action_kind", "issues[].segment", "issues[].category",
        "activity", "generated_at", "errors", "unavailable"]),
    ("/admin/api/messaging/health", ["sms.by_status", "sms.account_errors", "sms.attempted", "storm_caps"]),
    ("/admin/api/clients/list?segment=all&sort=health&page=1&per_page=50",
     ["items[]." + k for k in ("id", "name", "brand", "location_name", "segment", "is_demo", "health", "status", "monthly",
                               "mrr_source", "billed_by", "owner", "owner_email", "last_active", "on_ios", "created_at",
                               "issues", "critical", "churn", "setup", "onboarding", "deletion_due")]
     + ["counts.health", "counts.status", "counts.segment", "page", "pages", "total"]),
    ("/admin/api/onboarding", ["rows[]." + k for k in ("id", "name", "owner", "owner_email", "created_at", "days_since_signup",
                                                        "done", "total", "steps", "owner_hid_card", "status", "health",
                                                        "billed_by", "deletion_due")]),
    ("/admin/api/billing", ["rows[]." + k for k in (
        "restaurant_id", "restaurant", "owner", "owner_email", "status", "monthly", "mrr_source", "billed_by",
        "committed_monthly", "list_mismatch", "status_mismatch", "live", "stripe_customer_id", "pause_reason",
        "pause_reason_inferred", "paused_until", "contract_status", "signed_at", "days_since_signed", "converted_at",
        "days_in_trial", "margin", "segment", "deletion")]
     + ["totals.mrr", "totals.mrr_source", "totals.mrr_committed", "totals.mrr_past_due", "totals.mrr_fallback_accounts",
        "totals.subscriptions", "totals.covered_locations", "totals.mrr_basis", "margin.arpa", "margin.gross_margin",
        "margin.gross_margin_pct", "margin.basis", "events", "mirror_available"]),
    ("/admin/api/billing/health", ["webhooks", "reconcile", "failed_sends", "owed_pending.count", "owed_pending.oldest",
                                   "pipeline"]),
    ("/admin/api/vendor-costs?months=6", ["by_month[].month", "by_month[].entered_total", "by_month[].usage_total",
                                          "by_month[].total", "vendors", "note"]),
    ("/admin/api/issues/list?segment=attention&sort=severity&page=1&per_page=40",
     ["items[]." + k for k in ("key", "restaurant_id", "title", "severity", "since", "since_at", "occurrence_at",
                               "resolvable", "action", "action_kind", "action_href", "action_route", "action_payload",
                               "segment", "category", "restaurant")]
     + ["counts.attention", "counts.customer", "page", "pages", "total"]),
    ("/admin/api/bug-reports?status=open&limit=100", ["reports[]." + k for k in (
        "id", "message", "restaurant", "restaurant_id", "username", "email", "created_at", "source", "notified",
        "status", "meta")] + ["open"]),
    ("/admin/api/deletion-requests", ["requests[]." + k for k in (
        "restaurant_id", "name", "billing_status", "outstanding", "requested_on", "due_on", "days_left", "overdue")]),
    ("/admin/api/issues/resolutions", ["resolved", "total", "history"]),
    ("/admin/api/client/{rid}", ["client." + k for k in (
        "segment", "deletion", "on_ios", "team_last_seen", "activity", "logins", "owner", "integrations", "modules",
        "issues", "onboarding", "last_active")]
     + ["client.billing." + k for k in ("billed_by", "covers", "pause_reason", "paused_until", "days_in_trial",
                                        "signed_at", "converted_at", "mrr_source", "contract_status")]
     + ["client.churn_risk.scored", "client.reviews.draft_ready", "client.reviews.no_draft", "client.reviews.stalled",
        "client.integrations[].source", "client.integrations[].revoked_at", "client.integrations[].error_since",
        "query_errors", "errors", "profile.timezone", "data_sources", "staff_notes", "issues_resolved", "timeline_url",
        "siblings", "emails", "pushes", "devices", "alerts", "activity", "sessions", "schedules", "job_runs", "jobs",
        "ai_quality", "webhook_deliveries", "scheduled_posts", "events", "ai.daily", "generated_at"]),
    ("/admin/api/billing/{rid}", ["billing_status", "pause_reason", "pause_lock", "hold", "paused_until", "converted_at",
                                  "contract_status", "contract_signed_at", "stripe_customer_id", "modules", "billed_by",
                                  "covers", "subscription.status", "subscription.amount_cents", "subscription.billed_mrr",
                                  "open_invoice", "invoices", "history", "owed_sends", "envelopes", "reconcile"]),
    ("/admin/api/client/{rid}/billing/live", ["subscriptions"]),
    ("/admin/api/users/{owner}/security", ["user.username", "two_factor.scope", "two_factor.enabled", "two_factor.method",
                                           "two_factor.backup_codes_left", "lockout.locked", "lockout.seconds_left",
                                           "lockout.failures_15m", "lockout.failures_24h", "lockout.addresses",
                                           "sessions", "trusted_devices"]),
    ("/admin/api/client/{closing}/offboarding", ["request.requested_on", "request.due_on", "request.days_left",
                                                 "request.overdue", "steps[].step", "steps[].label", "steps[].hint",
                                                 "steps[].status", "outstanding", "ready_to_delete", "connected"]),
    ("/admin/api/client/{rid}/notes", ["notes[].author", "notes[].body", "notes[].created_at", "next_before_id",
                                       "legacy_internal_notes"]),
    ("/admin/api/client/{rid}/timeline?limit=60", ["events[].at", "events[].type", "events[].label", "events[].tone",
                                                   "next_before"]),
    ("/admin/api/client/{rid}/audit", ["events", "next_before_id"]),
    ("/admin/api/client/{rid}/audits", ["audits[].id", "audits[].restaurant_name", "audits[].audit_date",
                                        "audits[].status"]),
    ("/admin/api/audits", ["audits[].id", "audits[].restaurant_name"]),
    ("/admin/api/ai/client/{rid}?days=30", ["budget.ai.day.spend", "budget.ai.day.budget", "budget.ai.day.pct",
                                            "budget.ai.day.resets_at", "budget.ai.month.warn", "budget.ai.month.over",
                                            "budget.places.day", "budget.tier", "by_action", "blocked", "recent_calls",
                                            "quality.drafts", "quality.ask", "quality.safety", "quality.events",
                                            "stalled_reviews.unanalysed", "stalled_reviews.undrafted", "stalled_total"]),
    ("/admin/api/suppressions?restaurant_id={rid}", ["suppressions"]),
    ("/admin/api/sms?restaurant_id={rid}&limit=100", ["rows[].to_last4", "rows[].status", "rows[].use_case",
                                                      "rows[].error_code", "stats.by_status", "stats.account_errors"]),
    ("/admin/api/client/{rid}/brief-deliveries", ["deliveries"]),
    ("/admin/alert-contacts/{rid}", ["contacts"]),
    ("/admin/api/templates/{rid}", ["templates"]),
    ("/admin/api/brand/{rid}", ["version", "brand_name", "brand_color", "brand_logo_url", "exclude_from_learning",
                                "profile.service_model", "profile.concept", "profile.bar_led", "profile.ownership",
                                "profile.opened_year", "profile.profile_source"]),
    ("/admin/api/search?q=cus_UI1TEST", ["results[].match", "results[].type", "results[].id", "results[].title"]),
]


@pytest.mark.parametrize("path,fields", READS, ids=[p.split("?")[0] for p, _f in READS])
def test_every_read_the_console_makes_answers_with_the_fields_it_reads(world, path, fields):
    url = path.format(rid=world["rid"], owner=world["owner"], closing=world["closing"])
    r = world["c"].get(url)
    assert r.status_code == 200, (url, r.status_code, r.get_data(as_text=True)[:300])
    body = r.get_json()
    for f in fields:
        _has(body, f)


def test_the_badges_read_never_counts_an_internal_account(world):
    """The rail's attention count is customer + platform only (#141)."""
    d = world["c"].get("/admin/api/badges").get_json()
    c = d["counts"]
    assert c["attention"] == c["customer"]["total"] + c["platform"]["total"]
