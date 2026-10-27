"""The cavnar-watchdog Worker (workers/watchdog/worker.js), run in Node with
its KV, clock and fetch stubbed (Audit #25 A07-09).

/health answers 200 while the platform is degraded, and every page about a
degraded platform came from inside the web process over the app's own email
and text keys. The Worker now alerts when the body has not said
"status":"ok" for 30 minutes — once — and once more when it clears.
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

HARNESS = r"""
(async () => {
  const mod = await import(process.argv[2]);
  const worker = mod.default;
  const kv = new Map();
  const env = { WATCH: { get: async (k) => (kv.has(k) ? kv.get(k) : null), put: async (k, v) => { kv.set(k, v); } },
                PAGER_WEBHOOK_URL: "https://pager.test/hook", PING_TOKEN: "tok" };
  let now = Date.parse("2026-10-26T12:00:00Z");
  Date.now = () => now;
  const sent = [];
  let health = JSON.parse(process.argv[3]);
  globalThis.fetch = async (url, opts) => {
    if (String(url).includes("/health")) return { status: 200, json: async () => health };
    sent.push(JSON.parse(opts.body));
    return { ok: true };
  };
  const tick = async () => { let p; await worker.scheduled({}, env, { waitUntil: (x) => { p = x; } }); await p; };
  const counts = [];
  for (let i = 0; i < 8; i++) {
    await worker.fetch(new Request("https://watchdog.test/ping/tok"), env);
    await tick();
    counts.push(sent.length);
    now += 5 * 60000;
  }
  health = { status: "ok", db: "ok" };
  await worker.fetch(new Request("https://watchdog.test/ping/tok"), env);
  await tick();
  counts.push(sent.length);
  console.log(JSON.stringify({ counts, sent, state: JSON.parse(kv.get("state") || "null") }));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run(health):
    out = subprocess.run(["node", "-", "file://" + WORKER, json.dumps(health)], input=HARNESS, text=True,
                         capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_degraded_health_alerts_once_after_30_minutes_and_once_when_it_clears():
    got = _run({"status": "degraded", "db": "ok", "problems": ["The nightly backup is 30 hours old."]})
    # Checks every 5 minutes from 12:00: the 12:30 check is the first past 30.
    assert got["counts"] == [0, 0, 0, 0, 0, 0, 1, 1, 2], got["counts"]
    alert, cleared = got["sent"]
    assert alert["event"] == "trigger" and alert["subject"] == "Cavnar AI is degraded"
    assert "The nightly backup is 30 hours old." in alert["text"]
    assert cleared["event"] == "resolve" and "no longer degraded" in cleared["subject"]
    assert got["state"]["degraded_since"] is None and got["state"]["degraded_alerted"] is False


def test_a_healthy_body_never_alerts():
    got = _run({"status": "ok", "db": "ok"})
    assert got["counts"] == [0] * 9 and got["sent"] == []
