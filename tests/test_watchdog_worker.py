"""The cavnar-watchdog Worker (workers/watchdog/worker.js), run in Node with
its KV, clock and fetch stubbed (Audit #25 A07-09; re-audit B7 P3).

/health answers 200 while the platform is degraded, and every page about a
degraded platform came from inside the web process over the app's own email
and text keys. The Worker alerts when the body has not said "status":"ok"
on 5 of its last 7 checks (30 minutes of them) — a flapping body too — once,
and once more when it clears (two healthy checks running). Degraded turning
down resolves the degraded incident and escalates at once to the down alert.
A change to worker.js goes live only with `npx wrangler deploy` from
workers/watchdog/ (a lead step).
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "workers", "watchdog", "worker.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# argv[3]: the /health answer of each check in turn — a body (status 200), or
# a number (that HTTP status). A heartbeat lands before every check.
HARNESS = r"""
(async () => {
  const mod = await import(process.argv[2]);
  const worker = mod.default;
  const kv = new Map();
  const env = { WATCH: { get: async (k) => (kv.has(k) ? kv.get(k) : null), put: async (k, v) => { kv.set(k, v); } },
                PAGER_WEBHOOK_URL: "https://pager.test/hook", PAGERDUTY_ROUTING_KEY: "rk", PING_TOKEN: "tok" };
  let now = Date.parse("2026-10-26T12:00:00Z");
  Date.now = () => now;
  const sent = [];
  let health = null;
  globalThis.fetch = async (url, opts) => {
    if (String(url).includes("/health")) {
      if (typeof health === "number") return { status: health, json: async () => null };
      return { status: 200, json: async () => health };
    }
    const body = JSON.parse(opts.body);
    body.url = String(url);
    sent.push(body);
    return { ok: true };
  };
  const tick = async () => { let p; await worker.scheduled({}, env, { waitUntil: (x) => { p = x; } }); await p; };
  const counts = [];
  for (const h of JSON.parse(process.argv[3])) {
    health = h;
    await worker.fetch(new Request("https://watchdog.test/ping/tok"), env);
    await tick();
    counts.push(sent.filter((s) => s.url.includes("pager.test")).length);
    now += 5 * 60000;
  }
  console.log(JSON.stringify({ counts, sent, state: JSON.parse(kv.get("state") || "null") }));
})().catch((e) => { console.error(e); process.exit(1); });
"""

OK = {"status": "ok", "db": "ok"}
DEG = {"status": "degraded", "db": "ok", "problems": ["The nightly backup is 30 hours old."]}


def _run(steps):
    out = subprocess.run(["node", "-", "file://" + WORKER, json.dumps(steps)], input=HARNESS, text=True,
                         capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    got["hooks"] = [s for s in got["sent"] if "pager.test" in s["url"]]
    got["pd"] = [s for s in got["sent"] if "pagerduty" in s["url"]]
    return got


def test_a_degraded_health_alerts_once_after_30_minutes_and_once_when_it_clears():
    got = _run([DEG] * 8 + [OK, OK])
    # Checks every 5 minutes from 12:00: the 12:30 check (the 7th) is the first with a full window.
    assert got["counts"] == [0, 0, 0, 0, 0, 0, 1, 1, 1, 2], got["counts"]
    alert, cleared = got["hooks"]
    assert alert["event"] == "trigger" and alert["subject"] == "Cavnar AI is degraded"
    assert "The nightly backup is 30 hours old." in alert["text"]
    assert cleared["event"] == "resolve" and "no longer degraded" in cleared["subject"]
    assert got["state"]["degraded_since"] is None and got["state"]["degraded_alerted"] is False


def test_a_healthy_body_never_alerts():
    got = _run([OK] * 9)
    assert got["counts"] == [0] * 9 and got["sent"] == []


def test_a_flapping_health_alerts_where_one_healthy_check_used_to_reset_the_clock():
    got = _run([DEG, DEG, OK, DEG, DEG, DEG, OK, DEG, DEG])
    assert got["hooks"] and got["hooks"][0]["subject"] == "Cavnar AI is degraded", got["counts"]
    assert got["counts"][6] == 0 and got["counts"][7] == 1, "5 of the last 7 checks degraded, this one too"


def test_degraded_turning_down_resolves_the_degraded_incident_and_escalates_at_once():
    got = _run([DEG] * 7 + [503, 503])
    events = [(s["event_action"], s["dedup_key"]) for s in got["pd"]]
    assert events[0] == ("trigger", "cavnar-watchdog-degraded")
    assert ("resolve", "cavnar-watchdog-degraded") in events, events
    assert ("trigger", "cavnar-watchdog-down") in events, events
    # The down alert went on the FIRST down check (the 8th), not after two.
    assert got["counts"][7] == 3, got["counts"]
    assert got["state"]["degraded_alerted"] is False and got["state"]["alerted"] is True
