"""Fix round, UI wave 2 — the admin console's Operations, Engineering and
Analytics (templates/admin.html).

Three halves:

- Source rules, read from the template (the suite has no JS engine): every
  write control in these areas is `.w` (hidden from a support login); Run
  now refreshes the page it is on; no "every key is configured" or "since
  the last deploy" survives; the incident keys come from the server.
- Behaviour, run under node where it is installed: the page's own
  functions, given the payloads the server sends — a read that failed is
  "unknown" and the area never says "Everything ran" over it (#128); a
  failed, stuck or overdue job, a wedged scheduler, an AI outage turn their
  systems red (#158); partial runs and a failed admin task are never green
  (#40, #160); failed jobs are kind='job' only (#58).
- A render check: the Flask app built as the admin tests build it, signed in
  as an admin, GETs /admin and every JSON read these areas make, and asserts
  each answers 200 with the fields the page reads — a renamed field fails
  here, not on Will's screen.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zlib

import pytest
from flask import Flask

import admin_ops
import admin_routes
import auth
import models
from auth import create_session, create_user, init_auth
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
    """One top-level function's source, to the next top-level function or banner."""
    m = re.search(r"\n(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"{name}() is gone from admin.html"
    nxt = re.search(r"\n(?:async\s+)?function\s+\w+\s*\(|\n// ──|\nconst |\nlet ", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src))]


def _const(src, name):
    m = re.search(r"\n(const " + re.escape(name) + r" = [^\n]*)", src)
    assert m, f"const {name} is gone from admin.html"
    return m.group(1)


# The regions this wave owns (UI-2): Operations, Engineering, Analytics and
# the sections after them, the incident modal and the call-trace modal,
# runJob, the incident opener, and the System section's incident and
# changelog writes.
def _mine(src):
    return "\n".join([
        _between(src, "// ── Operations", "// ── Clients"),
        _between(src, "// ── Intelligence", "// ── Alerts & notifications"),
        _between(src, '<div class="mo" id="m-incident">', '<div class="toast"'),
        _fn(src, "runJob"), _fn(src, "incidentOpen"), _fn(src, "postIncident"),
        _fn(src, "changelogPost"), _fn(src, "changelogDelete"),
    ])


# ── every write control is a .w, and says what it does ──────────────────────

WRITE_FNS = ("runJob", "supReinstate", "stormLift", "capOpen", "aiResetBreaker", "aiCallOpen", "svcEdit",
             "saveService", "incidentOpen", "incidentUpdate", "incidentResolve", "postIncident", "changelogPost",
             "changelogDelete", "seedReviewAccount", "reviewAccountRotate", "meRename", "supportAdd", "lockClear",
             "expPin", "expPromote", "expRevert", "expPinForm", "resolveOpen", "act", "toastOpen", "rpowerOpen")


def test_every_write_control_in_these_areas_is_marked_w(page):
    mine = _mine(page)
    pat = re.compile(r"<(button|a)\b([^<>]*?)onclick=\"(?:event\.stopPropagation\(\);)?(?:_opsFresh=true;)?"
                     r"(" + "|".join(WRITE_FNS) + r")\(")
    bad, seen = [], 0
    for m in pat.finditer(mine):
        seen += 1
        cls = re.search(r'class="([^"]*)"', m.group(2))
        if not cls or not re.search(r"(^|\s)w(\s|$|\$\{)", cls.group(1)):
            bad.append(mine[m.start():m.start() + 100])
    assert seen > 25, "the write controls moved"
    assert not bad, "write controls not marked .w:\n" + "\n".join(bad[:8])
    # Forms that write are hidden whole for a support login.
    assert '<div class="fl w"><textarea id="iu-' in mine
    assert '<div class="fl w"><label>A new sign-in name' in mine
    assert '<div class="fl w" style="padding:14px 18px' in mine


def test_writes_confirm_and_say_the_servers_own_answer(page):
    w = _fn(page, "opsWrite")
    assert "confirm(question)" in w and "say(d, okText)" in w
    assert "the outcome is unknown" in w                     # a lost answer is never "done"
    # Each confirm names what it changes.
    assert "Reinstate ${email}?" in _fn(page, "supReinstate")
    assert "Reset the breaker for ${label}?" in _fn(page, "aiResetBreaker")
    assert "Change your sign-in name from ${_meName} to ${name}?" in _fn(page, "meRename")
    assert "A set-your-password link goes to ${em}" in _fn(page, "supportAdd")
    assert "Clear the sign-in lockout on ${name}?" in _fn(page, "lockClear")
    assert "Pin ${who} to ${arm}" in _fn(page, "expPin")
    assert "to the public status page" in _fn(page, "postIncident")
    assert "Every client sees it" in _fn(page, "changelogPost")


# ── Operations → Jobs (D, #95, #40, #9, #58, #160) ──────────────────────────

def test_run_now_refreshes_the_jobs_page_at_its_new_address(page):
    rj = _fn(page, "runJob")
    assert "=== 'jobs'" not in rj and "==='jobs'" not in rj          # the old address never matched (#95)
    assert "onJobsPage()" in rj and "_opsFresh = true; route()" in rj
    assert "say(d," in rj                                              # the server's message or 409 sentence
    on = _fn(page, "onJobsPage")
    assert "'operations/jobs'" in on and "'operations'" in on


def test_run_now_is_off_for_a_sending_job_where_the_server_refuses_it(page):
    b = _fn(page, "runBtn")
    assert "j.sends && d.local_sends_refused" in b and "disabled" in b and "local_sends_refused_reason" in b
    assert "d.local_sends_refused_reason" in _fn(page, "opsJobs")


def test_the_jobs_tab_reads_the_registry_rows_and_the_liveness(page):
    j = _fn(page, "opsJobs")
    for key in ("d.jobs", "d.heartbeat", "h.wedged", "h.loop_stalled", "d.lease", "d.backup", "d.operator_alert",
                "d.missed_windows", "d.dsr_missing", "d.requests", "d.inflight"):
        assert key in j, key
    row = _fn(page, "jobRow")
    for key in ("j.history", "j.sla_minutes", "j.stuck_after_minutes", "j.overdue", "j.runs_7d", "j.partial_7d"):
        assert key in row, key
    assert "/admin/api/jobs/' + encodeURIComponent(job) + '/runs" in _fn(page, "jobRuns")
    # Resolving a job's failures is for the occurrence the page saw (#24): its newest failure.
    assert "resolveOpen('job:${jsq(g.job)}','Job ${jsq(g.job)} failures','${jsq(g.last_at || '')}')" in j


def test_partial_and_running_are_never_green(page):
    assert "partial:'warn'" in _const(page, "JOB_PILL").replace(" ", "")
    assert "running:'emb'" in _const(page, "JOB_PILL").replace(" ", "")
    assert "partial:'p'" in _const(page, "RUN_SQ").replace(" ", "") and "running:'r'" in _const(page, "RUN_SQ").replace(" ", "")
    assert ".sq i.p{background:var(--amber)" in page and ".sq i.r{background:var(--ember2)" in page
    tw = _const(page, "TASK_WORD").replace(" ", "")
    assert "error:['bad'" in tw and "pending:['emb'" in tw          # an errored admin task is red (#160)


# ── the system state, run in node (#128, #158) ──────────────────────────────

def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _state_js(page, payloads):
    fns = "".join(_fn(page, n) for n in ("unknownV", "readErr", "issueTone", "opsState", "opsSentence", "failKinds",
                                         "runBtn", "histSquares", "rasFig", "fillUtcDays"))
    consts = "\n".join(_const(page, n) for n in ("ST_WORD", "JOB_WORD", "JOB_PILL", "RUN_SQ"))
    stubs = ("const esc = v => String(v == null ? '' : v); const jsq = esc; const fmtN = n => n == null ? '—' : String(n);"
             "const fmtUSD = n => '$' + n; const fmtDT = s => String(s); function pill(t, x){ return '<pill ' + t + '>' + x + '</pill>'; }")
    return stubs + "\n" + consts + "\n" + fns + "\nconst P = " + json.dumps(payloads) + ";\n"


FAILED = {"ok": False, "_status": 500, "error": "database is locked"}


def test_a_failed_read_is_unknown_and_the_area_never_says_all_clear(page):
    js = _state_js(page, {"o": {"ov": FAILED, "jb": FAILED, "mh": FAILED, "ai": FAILED}})
    out = _node(js + "const s = opsState(P.o); console.log(JSON.stringify({tones: s.map(x => [x.key, x.tone]), said: opsSentence(s)}));")
    assert all(t == "neu" for _k, t in out["tones"]), out["tones"]
    assert "Everything ran" not in out["said"] and "couldn't be read" in out["said"]


def test_a_healthy_platform_says_so_only_when_every_read_worked(page):
    ov = {"ok": True, "kpis": {"email_failures_today": 0, "push_failures_today": 0, "integrations_failing": 0,
                               "ai_failures_24h": 0, "ai_quality_24h": 0, "email_rates": {}}, "issues": []}
    jb = {"ok": True, "jobs": [{"state": "ok"}, {"state": "running"}], "heartbeat": {"beat_age_minutes": 2, "stale": False},
          "missed_windows": [], "dsr_missing": []}
    mh = {"ok": True, "problems": [], "sms": {"by_status": {"delivered": 3}, "account_errors": 0, "attempted": 3},
          "storm_caps": [], "push_outbox": {"failed_24h": 0}}
    ai = {"ok": True, "health": {"status": "operational"}}
    js = _state_js(page, {"good": {"ov": ov, "jb": jb, "mh": mh, "ai": ai}, "noMh": {"ov": ov, "jb": jb, "mh": FAILED, "ai": ai}})
    out = _node(js + "console.log(JSON.stringify([opsSentence(opsState(P.good)), opsSentence(opsState(P.noMh))]));")
    assert "Everything ran" in out[0]
    assert "Everything ran" not in out[1] and "SMS" in out[1]       # messaging health failed: SMS is unknown


def test_failing_jobs_a_wedged_scheduler_and_an_ai_outage_turn_red(page):
    ov = {"ok": True, "kpis": {"integrations_failing": 2, "email_rates": {}}, "issues": [
        {"key": "platform:error_rate", "severity": "critical", "title": "9% of requests are 5xx"},
        {"key": "email:bounce_rate", "severity": "warning", "title": "Email bounce rate 4% this week"}]}
    jb = {"ok": True, "jobs": [{"state": "failed"}, {"state": "partial"}, {"state": "ok"}],
          "heartbeat": {"beat_age_minutes": 3, "wedged": True, "running_job": "review_fetch", "running_minutes": 250,
                        "running_bound_minutes": 200}, "missed_windows": [], "dsr_missing": []}
    ai = {"ok": True, "health": {"status": "outage", "reason": "AI drafting is paused"}}
    mh = {"ok": True, "problems": ["3 text(s) failed in the last hour with a Twilio account-level error"],
          "sms": {"by_status": {}, "account_errors": 3, "attempted": 3}, "storm_caps": [], "push_outbox": {}}
    js = _state_js(page, {"o": {"ov": ov, "jb": jb, "mh": mh, "ai": ai}})
    out = _node(js + "console.log(JSON.stringify(Object.fromEntries(opsState(P.o).map(x => [x.key, x.tone]))));")
    assert out == {"scheduler": "bad", "jobs": "bad", "api": "bad", "email": "warn", "sms": "bad", "push": "good",
                   "ai": "bad", "integrations": "bad"}, out


def test_failed_jobs_are_kind_job_and_the_rest_is_listed_apart(page):
    d = {"failures": [{"job": "admin_console", "kind": "request", "error": "locked"},
                      {"job": "backup_db", "kind": "job", "error": "no off-site copy"},
                      {"job": "legacy", "error": "no kind column yet"}],
         "grouped": [{"job": "admin_console", "n": 4}, {"job": "backup_db", "n": 1}, {"job": "legacy", "n": 1}]}
    out = _node(_state_js(page, {"d": d}) + "const k = failKinds(P.d); console.log(JSON.stringify({jobs: k.jobs.map(f => f.job), other: k.other.map(f => f.job), groups: k.groups.map(g => g.job)}));")
    assert out == {"jobs": ["backup_db", "legacy"], "other": ["admin_console"], "groups": ["backup_db", "legacy"]}


def test_a_sending_job_cannot_be_run_here_and_says_why(page):
    js = _state_js(page, {"j": {"job": "review_fetch", "sends": True, "runnable": True, "label": "Review fetch"},
                          "d": {"local_sends_refused": True, "local_sends_refused_reason": "Not the production scheduler."},
                          "d2": {"local_sends_refused": False}})
    out = _node(js + "console.log(JSON.stringify([runBtn(P.j, P.d, false), runBtn(P.j, P.d2, false)]));")
    assert "disabled" in out[0] and "Not the production scheduler." in out[0] and "onclick" not in out[0]
    assert "runJob(" in out[1] and "disabled" not in out[1] and 'class="btn s w' in out[1]


def test_each_run_is_drawn_in_its_own_state(page):
    hist = [{"state": s, "started_at": "2026-09-29 10:00:00"} for s in ("running", "partial", "failed", "ok")]
    out = _node(_state_js(page, {"h": hist}) + "console.log(JSON.stringify(histSquares(P.h)));")
    cells = re.findall(r'<i class="([^"]*)"', out)
    assert len(cells) == 14 and cells[:10] == ["e"] * 10 and cells[10:] == ["", "x", "p", "r"]


def test_ai_days_keep_empty_days(page):
    out = _node(_state_js(page, {"rows": [{"day": "2000-01-01", "calls": 9}]}) + "console.log(JSON.stringify(fillUtcDays(P.rows, 7)));")
    assert len(out) == 7 and all("day" in d for d in out) and sum(d.get("calls", 0) for d in out) == 0


def test_a_withheld_score_shows_its_behaviour_only_part_never_a_zero(page):
    js = _state_js(page, {"a": {"ras": 47.5}, "b": {"ras": None, "ras_partial": 34.4, "ras_note": "withheld until measured"},
                          "c": {"ras": None, "ras_partial": None}})
    out = _node(js + "console.log(JSON.stringify([rasFig(P.a, 20), rasFig(P.b, 20), rasFig(P.c, 20)]));")
    assert "47.5" in out[0]
    assert "34.4" in out[1] and "behaviour only" in out[1] and "withheld until measured" in out[1]
    assert "needs 20+" in out[2]


# ── Email, push, AI, integrations: the fields the fix round added ───────────

def test_email_and_messaging_read_the_new_fields(page):
    e = _fn(page, "opsEmail")
    for key in ("d.rates", "rates.thresholds", "d.resend_webhook", "t.sent", "d.suppressed_total", "mh.problems",
                "mh.inbound_webhooks", "mh.sms", "mh.push_outbox", "mh.webhook_outbox", "/admin/api/sms?limit=50",
                "/admin/api/queues", "r.to_last4"):
        assert key in e, key
    assert "r.enough" in _fn(page, "rateCell")                      # judged only once there are enough sends
    s = _fn(page, "supLoad") + _fn(page, "supReinstate")
    assert "/admin/api/suppressions?limit=200" in s and "r.operator" in s and "r.scope" in s
    assert "'/admin/api/suppressions/reinstate', {email: email, reason: reason}" in s


def test_push_reads_storm_counts_auto_caps_and_opens_of_delivered(page):
    p = _fn(page, "opsPush")
    for key in ("d.storm_counts", "d.auto_caps_supported", "d.auto_caps", "mh.storm_caps", "d.posts_failed_total",
                "d.engagement_basis", "x.delivered", "stormLift("):
        assert key in p, key


def test_ai_reads_vendors_outcomes_ceilings_health_quality_and_the_trace(page):
    a = _fn(page, "opsAI")
    for key in ("d.by_vendor", "d.outcomes", "d.blocked", "d.rate_limit_hits", "p50_ms", "p95_ms", "b.global_month",
                "lim.warn_pct", "d.budget_watch", "d.health", "d.anomalies", "labels || {}).cost_card",
                "/admin/api/ai/quality?days=", "aiCallOpen("):
        assert key in a, key
    assert "'/admin/api/ai/reset-breaker'" in _fn(page, "aiResetBreaker")
    q = _fn(page, "aiQuality")
    for key in ("v.surfaces", "v.shadow", "v.trend", "q.models", "dr.needs_review_pct", "dr.review_reasons",
                "dr.edit_categories", "ask.helpful_pct", "sf.rate_pct", "per_100_calls", "q.unusable_outputs"):
        assert key in q, key
    assert "/admin/api/ai/calls?limit=50" in _fn(page, "aiCalls")
    c = _fn(page, "aiCallOpen")
    assert "/admin/api/ai/calls/" in c and "c.prompt" in c and "c.output" in c
    assert '<div class="mo" id="m-aicall">' in page


def test_integrations_read_revocation_webhooks_and_data_sources(page):
    i = _fn(page, "opsIntegrations")
    for key in ("r.revoked_at", "r.error_since", "d.webhooks_inbound", "/admin/api/data-sources", "x.summary",
                "x.stale_snapshots", "?fresh=1"):
        assert key in i, key


# ── Engineering (F, D, A, B2) ───────────────────────────────────────────────

def test_engineering_says_the_servers_warnings_never_every_key_is_configured(page):
    assert "Every key is configured" not in page
    e = _fn(page, "engineering")
    assert "sys.warnings" in e and "The system read failed" in e
    k = _fn(page, "engKeys")
    assert "k.breaks" in k and "k.state" in k and "Without it" in k
    sysc = _fn(page, "engSystem")
    for key in ("S.disk", "dk.low_below_mb", "dk.critical_below_mb", "db.journal", "wp.state", "S.volume", "vol.marker",
                "b.offsite", "S.drill", "S.credentials", "S.ai"):
        assert key in sysc, key
    for fn, keys in (("engProviders", ("x.streak", "x.last_ok_at", "x.checked_at")),
                     ("engErrors", ("server_errors_24h", "by_route", "e.latest")),
                     ("engLatency", ("rollups_24h", "saturated_minutes", "peak_inflight")),
                     ("engBoots", ("crash_loop", "size_trend")),
                     ("engSched", ("S.supervisor", "S.lease", "heartbeat_minutes")),
                     ("engBackup", ("bk.status", "bk.configured", "days_to_full", "offsite_error"))):
        body = _fn(page, fn)
        for key in keys:
            assert key in body, (fn, key)
    # Customer and console latency are separate lines (#77).
    lc = _fn(page, "latencyChart")
    assert "g('customer')" in lc and "g('admin')" in lc


def test_windows_are_labelled_never_since_the_last_deploy(page):
    mine = _mine(page)
    assert "since the last deploy" not in mine and "since deploy" not in mine
    assert "last 5 minutes" in _fn(page, "engApi") and "api_window_label" in _fn(page, "opsState")


def test_the_status_page_lists_incidents_with_update_and_resolve(page):
    s = _fn(page, "engStatus")
    assert "/admin/status/services" in s and "/admin/status/incidents" in s
    assert "sv.incident_statuses" in s and "i.updates" in s and "incidentUpdate(" in s and "incidentResolve(" in s
    assert "rewrite each service every few minutes" in s                   # a manual Change doesn't last
    assert "'/admin/status/incident/' + id + '/update'" in _fn(page, "incidentUpdate")
    assert "'/admin/status/incident/' + id + '/resolve'" in _fn(page, "incidentResolve")
    assert "next check" in _fn(page, "saveService")
    o = _fn(page, "incidentOpen")
    assert "/admin/status/services" in o and "d.services" in o
    assert "'storage'" in _const(page, "INC_KEYS")
    assert "onclick=\"postIncident()\"" in page


def test_the_audit_trail_and_refused_attempts_page_by_id(page):
    a = _fn(page, "audLoad")
    for key in ("/admin/api/audit?limit=50", "restaurant_id=", "actor=", "action=", "result=", "all=1", "before_id=",
                "d.next_before_id"):
        assert key in a, key
    assert "/admin/api/audit/refused?limit=50" in _fn(page, "refLoad")
    r = _fn(page, "audRow")
    assert "e.before" in r and "e.after" in r and "e.request" in r


def test_team_and_access(page):
    a = _fn(page, "engAccess")
    assert "/admin/api/support-logins" in a and "/admin/api/lockouts" in a and 'href="/admin/two-factor"' in a
    assert "'/admin/api/me/username'" in _fn(page, "meRename")
    assert "'/admin/api/support-logins'" in _fn(page, "supportAdd")
    assert "'/clear-lockout'" in _fn(page, "lockClear")


def test_the_review_account_keeps_its_password_and_rotation_is_typed_out(page):
    e = _fn(page, "engineering")
    assert 'id="seed-btn" onclick="seedReviewAccount()"' in e and "reviewAccountRotate()" in e
    rot = _fn(page, "reviewAccountRotate")
    assert "'ROTATE'" in rot and "rotate_password: true, confirm: 'ROTATE'" in rot
    assert "pollJob(jobUrl(d.job_id))" in rot and "jobSaid(r," in rot


def test_stop_viewing_is_gone_from_the_tools(page):
    assert "Stop viewing as client" not in page and 'href="/admin/stop-viewing"' not in page


# ── Analytics (C) ───────────────────────────────────────────────────────────

def test_analytics_reads_adoption_business_metrics_and_labels_what_is_missing(page):
    a = _fn(page, "analytics")
    assert "/admin/api/adoption" in a and "/admin/api/business-metrics?days=" in a
    assert "m.paying" in a and "m.trial" in a and "tc.available" in a        # paying and trial apart (#71)
    assert "not measured yet" in a and "rec.internal_excluded" in a
    # Accounts as the slim server-paged rows, not the full records (#79).
    assert "customerRows()" in a and "loadClients(" not in a
    assert "/admin/api/clients/list?segment=customer" in _fn(page, "customerRows")
    h = _fn(page, "anaHistory")
    assert "nothing before it is reconstructed" in h and "bm.first_date" in h
    assert "a gap in the lines" in h                                  # a missed night is a gap, not a zero
    r = _fn(page, "recommendations")
    assert "r.ras_partial" in r and "T.ras_partial" in r and "not measured" in r and "d.internal_excluded" in r
    i = _fn(page, "intelligence")
    assert "sv.periods" in i and "sv.as_of" in i and "sv.source" in i and "unknownV(" in i and "dayOf(" in i


def test_calendar_days_are_never_shifted_or_iso(page):
    mine = _mine(page)
    assert "toLocaleDateString" not in mine
    # A calendar day ("2026-09-29") is built as a local date, never parsed as UTC.
    for fn in ("opsJobs", "opsEmail", "anaHistory", "intelligence"):
        assert "dayOf(" in _fn(page, fn), fn


# ── the render check ─────────────────────────────────────────────────────────

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
    import push, webhooks, guest_marketing, sales_audits, ops, platform_monitor, provider_health, ai_utils
    push.init_push(db_path)
    webhooks.init_webhooks(db_path)
    guest_marketing.init_guest_marketing(db_path)
    sales_audits.init_sales_audits(db_path=db_path)
    for init in (ops.init_ops, admin_ops.init_admin_ops, platform_monitor.init_platform_tables,
                 provider_health.init_provider_health, ai_utils.init_ai_ops):
        init(db_path)
    admin_ops.invalidate_fleet_cache()

    home = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="will@ui2.test", billing_status="internal"),
                             db_path=db_path)
    admin = create_user(home, "will", "will@ui2.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    rid = create_restaurant(Restaurant(name="Test Grill", owner_email="owner@ui2.test", module_reviews=1, module_labor=1),
                            db_path=db_path)
    create_user(rid, "grill", "owner@ui2.test", "Owner-pass-2026", db_path=db_path)
    update_restaurant(rid, {"billing_status": "active"}, db_path=db_path)
    support = create_user(home, "helper", "helper@ui2.test", "Helper-pass-2026", role="support", db_path=db_path)
    conn = real(db_path)
    try:
        x = conn.execute
        for job, ok, err, ago in (("backup_db", 0, "BackupFailed: no off-site copy", 600), ("review_fetch", 2, None, 300),
                                  ("review_fetch", 1, None, 30), ("prune_ledgers", 1, None, 60)):
            x("INSERT INTO job_runs (job, started_at, finished_at, duration_ms, ok, error, context, result_json) VALUES "
              "(?, datetime('now', ?), datetime('now', ?), 1200, ?, ?, 'manual by will', '{\"attempted\": 3}')",
              (job, f"-{ago} minutes", f"-{ago - 1} minutes", ok, err))
        x("INSERT INTO job_failures (job, error, context, kind, restaurant_id) VALUES ('backup_db', 'no off-site copy', "
          "'', 'job', NULL)")
        x("INSERT INTO job_failures (job, error, context, kind) VALUES ('admin_console', 'database is locked', "
          "'query=email_log', 'request')")
        x("INSERT INTO email_log (restaurant_id, email_type, to_email, subject, status, error) VALUES "
          "(?, 'weekly_digest', 'owner@ui2.test', 'Your week', 'bounced', '550')", (rid,))
        x("INSERT INTO email_suppressions (email, reason, detail, scope) VALUES ('owner@ui2.test', 'bounced', '550', 'all')")
        x("INSERT INTO sms_log (restaurant_id, use_case, to_hash, to_last4, status, error_code, error) VALUES "
          "(?, 'alert', 'h', '4242', 'failed', '30007', 'Carrier filtered')", (rid,))
        x("INSERT INTO alert_storm_caps (restaurant_id, local_day, until_at, alerts_in_window, threshold) VALUES "
          "(?, '2026-09-29', datetime('now', '+6 hours'), 14, 10)", (rid,))
        for vendor, outcome, reason in (("anthropic", "ok", None), ("anthropic", "error", None),
                                        ("google_places", "ok", None), ("anthropic", "blocked", "budget")):
            x("INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, vendor, "
              "outcome, status, reason, latency_ms, attempts, call_id, correlation_id, \"trigger\") VALUES "
              "(?, 'draft_response', 'claude-sonnet-5', 100, 50, 0.02, ?, ?, ?, ?, 900, 1, 'abcdef0123456789', "
              "'job:x:1', 'scheduler')", (rid, vendor, outcome, "ok" if outcome == "ok" else "error", reason))
        x("INSERT INTO ai_calls (call_id, created_at, restaurant_id, action, vendor, model, \"trigger\", correlation_id, "
          "outcome, latency_ms, attempts, prompt_z, output_z) VALUES ('abcdef0123456789', datetime('now'), ?, "
          "'draft_response', 'anthropic', 'claude-sonnet-5', 'scheduler', 'job:x:1', 'ok', 900, 1, ?, ?)",
          (rid, zlib.compress(b"Write a reply."), zlib.compress(b"Thanks!")))
        x("INSERT INTO ai_quality_events (surface, kind, n, detail, restaurant_id) VALUES ('reviews', 'figures', 2, "
          "'a stated figure not in the input', ?)", (rid,))
        x("INSERT INTO backup_runs (started_at, finished_at, local_ok, integrity_ok, size_bytes, offsite_ok, "
          "offsite_error, db_bytes, wal_bytes, backups_bytes, free_bytes) VALUES (datetime('now','-1 day'), "
          "datetime('now','-1 day'), 1, 1, 7000000, 0, 'BACKUP_S3_* not set', 7000000, 100000, 30000000, 400000000)")
        x("INSERT INTO provider_health (provider, state, detail, http_status, latency_ms) VALUES "
          "('resend', 'failing', '401 invalid key', 401, 120)")
        for d, mrr in (("2026-09-24", 900), ("2026-09-26", 1200)):
            x("INSERT INTO business_metrics_daily (date, generated_at, mrr, committed_mrr, paying_accounts, "
              "trial_accounts, past_due_accounts, signups, churns, active_accounts_7d) VALUES (?, ?, ?, ?, 3, 1, 0, 1, "
              "0, 4)", (d, d + " 23:50:00", mrr, mrr + 300))
        x("INSERT INTO admin_events (source, event_type, actor, restaurant_id, summary, result) VALUES "
          "('admin', 'lockout_cleared', 'will', ?, 'will cleared a lockout', 'ok')", (rid,))
        conn.commit()
    finally:
        conn.close()
    import status_manager
    status_manager.create_incident("Email delays", "Resend is slow", ["email"], "degraded")

    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.jinja_env.filters["format_num"] = lambda v: v
    app.jinja_env.filters["format_date"] = lambda v: v
    from auth_routes import auth_bp
    from status_routes import status_bp
    for bp in (admin_routes.admin_bp, auth_bp, status_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    c.set_cookie("session_token", create_session(admin, db_path=db_path, password_verified_at=True))
    yield {"c": c, "rid": rid, "support": support, "db": db_path}
    admin_ops.invalidate_fleet_cache()


def test_the_console_page_renders_with_these_areas(world):
    r = world["c"].get("/admin")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for fn in ("async function operations(", "function opsJobs(", "function opsEmail(", "function opsPush(",
               "function opsAI(", "async function opsIntegrations(", "async function engineering(",
               "async function engStatus(", "async function engAudit(", "async function engAccess(",
               "async function analytics(", 'id="m-aicall"'):
        assert fn in html, fn
    assert "{{" not in html and "{%" not in html
    assert 'const ME = "will";' in html                                  # the admin's own name, from Jinja


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
    ("/admin/api/overview", ["kpis." + k for k in (
        "emails_today", "email_failures_today", "email_rates", "push_today", "push_failures_today", "ai_calls_today",
        "ai_failures_24h", "ai_quality_24h", "integrations_active", "integrations_failing")]
     + ["issues", "generated_at", "errors", "unavailable"]),
    ("/admin/api/jobs", ["jobs[]." + k for k in (
        "job", "label", "cadence", "what", "sends", "runnable", "sla_minutes", "stuck_after_minutes", "lane", "retry",
        "state", "last_ok_at", "avg_ms", "runs_7d", "failed_7d", "partial_7d", "running_since", "overdue", "history")]
     + ["heartbeat." + k for k in ("beat_age_minutes", "loop_completed_age_minutes", "running_job", "running_minutes",
                                   "running_bound_minutes", "wedged", "loop_stalled", "stale", "stale_after_minutes")]
     + ["lease", "backup.state", "backup.summary", "backup.offsite_targets", "backup.last_run_at", "operator_alert",
        "missed_windows", "dsr_missing", "requests", "inflight", "failures[].kind", "failures[].job",
        "failures[].restaurant_id", "grouped[].job", "grouped[].n", "grouped[].last_at", "grouped[].sample",
        "local_sends_refused", "local_sends_refused_reason"]),
    ("/admin/api/jobs/backup_db/runs?limit=50", ["runs[]." + k for k in (
        "started_at", "state", "duration_ms", "result_json", "manual", "context", "error")]),
    ("/admin/api/messaging/health", ["problems", "inbound_webhooks", "sms.by_status", "sms.account_errors",
                                     "sms.attempted", "push_outbox.by_state", "push_outbox.failed_24h",
                                     "push_outbox.oldest_pending_at", "webhook_outbox.by_state", "storm_caps[].restaurant_id",
                                     "storm_caps[].restaurant", "storm_caps[].until_at", "storm_caps[].alerts_in_window"]),
    ("/admin/api/emails", ["rows[].email_type", "rows[].status", "by_type[].n", "by_type[].failed", "by_type[].bounced",
                           "daily[].day", "daily[].n", "daily[].failed", "storms", "suppressed_total", "suppressed_by",
                           "today.sent", "today.failed", "today.bounced", "today.complained", "engagement",
                           "rates.thresholds.bounce_warn_pct", "rates.thresholds.bounce_crit_pct",
                           "rates.thresholds.complaint_warn_pct", "rates.thresholds.complaint_crit_pct",
                           "rates.thresholds.min_sends", "rates.7d.enough", "rates.7d.bounce_rate", "rates.7d.accepted",
                           "rates.30d.delivered", "resend_webhook.problem", "resend_webhook.stale",
                           "resend_webhook.last_verified_at", "errors"]),
    ("/admin/api/sms?limit=50", ["rows[].to_last4", "rows[].status", "rows[].use_case", "rows[].error_code",
                                 "rows[].error", "rows[].created_at", "rows[].restaurant_id", "stats"]),
    ("/admin/api/queues", ["queues[].key", "queues[].label", "queues[].available"]),
    ("/admin/api/suppressions?limit=200", ["suppressions[].email", "suppressions[].scope", "suppressions[].reason",
                                           "suppressions[].detail", "suppressions[].created_at", "suppressions[].operator"]),
    ("/admin/api/notifications", ["pushes", "devices", "devices_total", "alerts", "by_type", "storms", "caps",
                                  "storm_counts.today", "storm_counts.storm_days_7d", "storm_counts.restaurants_7d",
                                  "auto_caps", "auto_caps_supported", "scheduled_posts", "posts_failed_total",
                                  "posts_scheduled_total", "today_push", "engagement", "ignored", "engagement_basis"]),
    ("/admin/api/ai?days=30", ["totals." + k for k in ("calls", "cost", "tin", "tout", "ai_cost", "data_api_cost",
                                                      "blocked", "today", "month")]
     + ["labels.cost_card", "by_vendor[].vendor", "by_vendor[].label", "by_vendor[].kind", "by_vendor[].errors",
        "by_vendor[].blocked", "outcomes.ok", "outcomes.blocked", "blocked[].reason", "blocked[].vendor",
        "blocked[].last_at", "rate_limit_hits", "by_action[].outcomes", "by_action[].p50_ms", "by_action[].p95_ms",
        "by_action[].admin_cost", "by_client[].not_ok", "by_client[].data_api_cost", "daily[].day", "daily[].calls",
        "failures[].outcome", "failures[].sample", "recent[].outcome", "recent[].trigger", "recent[].call_id",
        "recent[].latency_ms", "recent[].attempts", "recent_failed[].call_id", "budget.global_month.pct",
        "budget.global_month.warn", "budget.global_month.over", "budget.global_month.resets_at",
        "budget.places_month.spend", "budget.limits.paid.day", "budget.limits.trial.month",
        "budget.limits.unpaid.day", "budget.limits.places.month", "budget.limits.warn_pct", "budget_watch",
        "health.status", "health.vendors.anthropic.breaker", "health.vendors.anthropic.key_configured",
        "health.vendors.anthropic.calls_1h", "health.vendors.anthropic.errors_1h",
        "health.vendors.anthropic.error_rate_1h", "health.vendors.anthropic.blocked_1h",
        "health.vendors.anthropic.last_ok_at", "anomalies", "quality", "failed.n", "failed.n_24h"]),
    ("/admin/api/ai/quality?days=30", ["validation.surfaces", "validation.shadow", "validation.trend", "models[].purpose",
                                       "models[].model", "models[].overridden", "models[].env", "drafts.needs_review_pct",
                                       "drafts.drafted", "drafts.review_reasons", "drafts.edit_categories",
                                       "ask.helpful_pct", "ask.rated", "safety.rate_pct", "safety.disagreements",
                                       "events[].surface", "events[].kind", "events[].per_100_calls", "events[].last_at",
                                       "unusable_outputs"]),
    ("/admin/api/ai/calls?limit=50", ["calls[]." + k for k in ("call_id", "created_at", "restaurant_id", "restaurant",
                                                               "action", "model", "outcome", "trigger", "latency_ms",
                                                               "text_kept", "stop_reason")]),
    ("/admin/api/ai/calls/abcdef0123456789", ["call.created_at", "call.action", "call.model", "call.restaurant_id",
                                              "call.outcome", "call.trigger", "call.correlation_id", "call.template_hash",
                                              "call.text_kept", "call.prompt", "call.output", "validation", "quality",
                                              "related"]),
    ("/admin/api/integrations", ["rows[]." + k for k in ("restaurant_id", "restaurant", "label", "state", "integration",
                                                         "error", "error_since", "revoked_at", "freshness",
                                                         "last_success", "segment")]
     + ["systemic", "webhooks_inbound.providers[].label", "webhooks_inbound.providers[].problem",
        "webhooks_inbound.providers[].stale", "webhooks_inbound.providers[].configured",
        "webhooks_inbound.providers[].last_verified_at", "webhooks_inbound.providers[].failures_24h",
        "webhooks_inbound.providers[].secret_env", "webhooks_inbound.ledger", "webhooks_inbound.basis",
        "generated_at", "errors"]),
    ("/admin/api/data-sources", ["rows", "summary", "data_health_median", "stale_snapshots", "issue_threshold"]),
    ("/admin/api/system", ["keys[]." + k for k in ("label", "vars", "group", "required", "state", "breaks")]
     + ["warnings", "build", "env", "db"]
     + ["system." + k for k in (
         "disk.state", "disk.free_mb", "disk.pct_free", "disk.total_mb", "disk.db_mb", "disk.wal_mb",
         "disk.low_below_mb", "disk.critical_below_mb", "database.journal", "database.write.state",
         "database.write.ms", "database.clients", "database.file", "volume.mount", "volume.marker", "backup.state",
         "backup.age_hours", "backup.size_mb", "backup.offsite", "backup.offsite_age_hours", "backup.error", "drill",
         "lease", "heartbeat_minutes", "supervisor", "ai.status", "http.now.by_class", "http.now.inflight.now",
         "http.now.inflight.threads", "http.now.inflight.peak_5m", "rollups_24h.hourly", "rollups_24h.classes",
         "rollups_24h.saturated_minutes", "rollups_24h.peak_inflight", "rollups_24h.threads",
         "server_errors_24h.total", "server_errors_24h.by_route", "server_errors_24h.latest",
         "boots.recent", "boots.crash_loop.looping", "boots.crash_loop.boots_last_hour", "boots.crash_loop.unclean",
         "size_trend.source", "size_trend.points", "providers.resend.state", "providers.resend.streak",
         "providers.resend.last_ok_at", "providers.resend.checked_at", "credentials.key", "credentials.plaintext",
         "credentials.encrypted")]),
    ("/admin/api/backup", ["status.state", "status.summary", "status.age_hours", "status.offsite_age_hours",
                           "status.last_error", "configured.s3", "configured.email", "runs[].started_at",
                           "runs[].local_ok", "runs[].integrity_ok", "runs[].size_bytes", "runs[].offsite_ok",
                           "runs[].offsite_target", "runs[].offsite_error", "runs[].free_bytes",
                           "storage.growth_bytes_per_day", "storage.days_to_full"]),
    ("/admin/api/schedule-experiments", ["experiments", "pins"]),
    ("/admin/status/services", ["services[].key", "services[].name", "services[].description", "statuses",
                                "effective", "service_statuses", "incident_statuses", "incident_severities"]),
    ("/admin/status/incidents", ["open[]." + k for k in ("id", "title", "severity", "status", "created_at",
                                                         "updated_at", "affected_keys", "updates", "body")]
     + ["resolved", "statuses", "severities"]),
    ("/admin/api/audit?limit=50", ["events[]." + k for k in ("id", "created_at", "action", "actor", "restaurant_id",
                                                             "restaurant", "target", "result", "summary", "before",
                                                             "after", "kind", "request", "ip", "request_id")]
     + ["next_before_id"]),
    ("/admin/api/audit/refused?limit=50", ["attempts", "next_before_id"]),
    ("/admin/api/support-logins", ["logins[]." + k for k in ("id", "username", "email", "is_active", "last_login",
                                                             "created_at", "two_fa_enabled")]),
    ("/admin/api/lockouts", ["lockouts"]),
    ("/admin/api/changelog", ["entries"]),
    ("/admin/api/adoption", ["modules[].module", "modules[].paying.using", "modules[].paying.entitled",
                             "modules[].paying.rate_pct", "modules[].trial.using", "trial_conversion.available",
                             "basis", "generated_at"]),
    ("/admin/api/business-metrics?days=90", ["rows[]." + k for k in ("date", "mrr", "committed_mrr", "paying_accounts",
                                                                     "trial_accounts", "past_due_accounts", "signups",
                                                                     "churns", "active_accounts_7d")]
     + ["first_date", "note"]),
    ("/admin/api/recommendations?days=30", ["total.n", "total.ras", "funnel[].step", "funnel[].n", "min_n", "weights",
                                            "internal_excluded", "by_restaurant", "by_kind", "most_ignored", "fatigue"]),
    ("/admin/api/intelligence", ["savings.periods", "savings.source", "savings.as_of", "savings.restaurants_counted",
                                 "savings.restaurants_active", "savings.note", "learning.weeks", "computed_at", "cached",
                                 "age_seconds", "floor"]),
    ("/admin/api/clients/list?segment=customer&sort=name&per_page=200&page=1",
     ["items[].status", "items[].last_active", "items[].churn", "items[].segment", "pages"]),
]


@pytest.mark.parametrize("path,fields", READS, ids=[p.split("?")[0] for p, _f in READS])
def test_every_read_these_areas_make_answers_with_the_fields_they_read(world, path, fields):
    r = world["c"].get(path)
    assert r.status_code == 200, (path, r.status_code, r.get_data(as_text=True)[:300])
    body = r.get_json()
    for f in fields:
        _has(body, f)


def test_the_writes_these_areas_make_answer_with_what_the_toast_says(world, monkeypatch):
    c = world["c"]
    # Run now: a job that sends is refused here with a sentence (the page shows `error`).
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    r = c.post("/admin/api/jobs/review_fetch/run")
    assert r.status_code == 409 and r.get_json()["error"]
    # Incidents: post, update, resolve.
    r = c.post("/admin/status/incident", json={"title": "Storage filling", "body": "", "severity": "outage",
                                               "affected_keys": ["storage"], "status": "investigating"})
    assert r.status_code == 200 and r.get_json()["ok"]
    iid = r.get_json()["id"]
    assert c.post(f"/admin/status/incident/{iid}/update", json={"message": "Watching.", "status": "monitoring"}).get_json()["ok"]
    r = c.post(f"/admin/status/incident/{iid}/resolve", json={"message": "Resolved."})
    assert r.get_json()["incident"]["status"] == "resolved"
    # Reinstate a suppression: its answer names the address.
    r = c.post("/admin/api/suppressions/reinstate", json={"email": "owner@ui2.test", "reason": "test"})
    assert r.status_code == 200 and r.get_json()["email"] == "owner@ui2.test"
    # Lift a storm cap.
    assert c.post(f"/admin/api/client/{world['rid']}/storm-cap/lift").status_code == 200
    # The breaker reset answers with every breaker's state.
    assert "breakers" in c.post("/admin/api/ai/reset-breaker", json={}).get_json()
    # A changelog entry.
    r = c.post("/admin/api/changelog", json={"title": "Faster fetches", "body": "", "tag": "improvement"})
    assert r.get_json()["ok"]
