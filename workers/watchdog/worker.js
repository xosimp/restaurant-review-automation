// cavnar-watchdog — the monitor that lives OUTSIDE Railway.
//
// 1. Dead-man heartbeat: the scheduler GETs /ping/<token> at the end of every
//    tick (ops.ping_healthcheck, HEALTHCHECK_PING_URL). Silence past
//    STALE_MINUTES pages Will.
//    /ping/<token>/fail marks a failed backup or digest. It is an EVENT, kept
//    in its own key until it has been alerted (scale re-audit 10/20/26
//    R8-13): it used to ride the heartbeat's key, so the next ordinary ping —
//    the end of the same tick — overwrote it seconds later, and the cron,
//    which needs two sightings five minutes apart, could never see it. A
//    failure now alerts on the first check that sees it, once, whatever the
//    heartbeat says, and expires after FAIL_KEEP_HOURS if no channel took it.
// 2. Uptime: every 5 minutes the cron fetches /health and expects 200 with
//    "db":"ok". Two failing checks in a row page Will (a deploy's restart is
//    shorter than that); a recovery sends one "back up" email.
// 3. Degraded (Audit #25 A07-09): /health is a 200 while the platform is
//    DEGRADED (a stale scheduler, a full disk, a stale backup, overdue jobs —
//    its `problems`), and every page about those comes from inside the web
//    process, over the app's own Twilio and Resend keys. A body without
//    "status":"ok" on DEGRADED_MIN of the last DEGRADED_WINDOW checks (30
//    minutes of them) alerts once from out here, naming the problems — a
//    FLAPPING /health too: one healthy check no longer starts the count
//    again (re-audit B7 P3, 10/27/26) — and once more when it clears (two
//    healthy checks running). Degraded turning DOWN resolves the degraded
//    incident and escalates at once to the down alert (it was left open, and
//    later degraded alerts were deduplicated into it).
// Alerts go by Resend from the app's own sender to every ALERT_TO address
// (comma-separated), and — so a Resend outage cannot silence the one
// monitor outside Railway (scale audit 10/19/26 OPS-4) — to every channel
// below that is configured, each off until its secrets are set:
//   PAGERDUTY_ROUTING_KEY      a PagerDuty Events v2 trigger, resolved on recovery
//                              (phone escalation and a second on-call live there)
//   PAGER_WEBHOOK_URL          any paging service's incoming webhook (Better Stack,
//                              Opsgenie, ...): POST {event, subject, text, source}
//   TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, ALERT_SMS_TO (comma-separated)
//                              a text to each on-call phone
// An alert counts as delivered when any channel took it. State lives in KV.
const HEALTH_URL = "https://dashboard.cavnar.ai/health";
const STALE_MINUTES = 20;
const FAILS_BEFORE_ALERT = 2;
// Workers KV's free tier allows 1,000 writes a day (10/1/26: the 50% alert).
// A heartbeat landed every scheduler tick and every in-job pulse (~80s), and
// the cron rewrote its state every 5 minutes - 600-1,000+ writes a day. Now
// a heartbeat is written at most once per PING_WRITE_MINUTES (a failure at
// once), and the state only when it changes: about 300 writes a day.
const PING_WRITE_MINUTES = 4;
// A failure not yet alerted is kept this long (a day and a half: past the
// next night's backup, which says the same again if it fails again).
const FAIL_KEEP_HOURS = 36;
// A degraded /health on DEGRADED_MIN of the last DEGRADED_WINDOW checks (five
// minutes apart: 30 minutes from the first to the last) alerts; it clears
// after DEGRADED_CLEAR_CHECKS healthy checks running.
const DEGRADED_WINDOW = 7;
const DEGRADED_MIN = 5;
const DEGRADED_CLEAR_CHECKS = 2;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const m = url.pathname.match(/^\/ping\/([A-Za-z0-9_-]+)(\/fail)?\/?$/);
    if (!m || m[1] !== env.PING_TOKEN) return new Response("not found", { status: 404 });
    const fail = Boolean(m[2]);
    if (fail) {
      // Its own key, never overwritten by a heartbeat; the cron alerts on it.
      await env.WATCH.put("fail", JSON.stringify({ at: Date.now(), alerted: false }),
        { expirationTtl: FAIL_KEEP_HOURS * 3600 });
    }
    const last = JSON.parse((await env.WATCH.get("ping")) || "null");
    // Reads are cheap (100,000 a day); a heartbeat write only when it is
    // older than the window (a /fail is a heartbeat too: the process is up).
    if (!last || Date.now() - last.at >= PING_WRITE_MINUTES * 60000) {
      await env.WATCH.put("ping", JSON.stringify({ at: Date.now() }));
    }
    return new Response("ok");
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(check(env));
  },
};

async function check(env) {
  const now = Date.now();
  const problems = [];
  const ping = JSON.parse((await env.WATCH.get("ping")) || "null");
  // A reported failure alerts at once, on its own (R8-13): one sighting.
  const failed = JSON.parse((await env.WATCH.get("fail")) || "null");
  if (failed && !failed.alerted) {
    const when = new Date(failed.at).toISOString().replace("T", " ").slice(0, 16) + " UTC";
    const sent = await notify(env, "trigger", "Cavnar AI: a nightly job failed",
      `The scheduler reported a failure at ${when} (the nightly backup or the failure digest). ` +
      "The in-app page has the reason; this is the copy from outside Railway.", "cavnar-watchdog-fail");
    if (sent) {
      await env.WATCH.put("fail", JSON.stringify({ at: failed.at, alerted: now }),
        { expirationTtl: FAIL_KEEP_HOURS * 3600 });
    }
  }
  if (ping && now - ping.at > STALE_MINUTES * 60000)
    problems.push(`No heartbeat from the scheduler for ${Math.round((now - ping.at) / 60000)} minutes — its jobs have stopped.`);
  let degraded = null;      // the /health problems while it answers 200 without "status":"ok"
  try {
    const r = await fetch(HEALTH_URL, { headers: { "User-Agent": "cavnar-watchdog" }, cf: { cacheTtl: 0 } });
    if (r.status !== 200) problems.push(`dashboard.cavnar.ai/health answered ${r.status}.`);
    else {
      const j = await r.json().catch(() => null);
      if (!j || j.db !== "ok") problems.push(`dashboard.cavnar.ai/health says the database is ${j ? j.db : "unreadable"}.`);
      else if (j.status !== "ok") degraded = (Array.isArray(j.problems) ? j.problems : []).map(String).slice(0, 6);
    }
  } catch (e) {
    problems.push(`dashboard.cavnar.ai/health could not be reached (${e.message}).`);
  }
  const prev = JSON.parse((await env.WATCH.get("state")) || '{"streak":0,"alerted":false}');
  // Degraded: the last DEGRADED_WINDOW checks (1 degraded, 0 not), so a
  // flapping body is counted, not restarted by its first healthy check.
  const windowBefore = Array.isArray(prev.degraded_window) ? prev.degraded_window.slice(-DEGRADED_WINDOW) : [];
  const degradedWindow = windowBefore.concat([degraded ? 1 : 0]).slice(-DEGRADED_WINDOW);
  const degradedCount = degradedWindow.reduce((a, b) => a + b, 0);
  let degradedSince = degraded ? (prev.degraded_since || now) : (degradedCount ? (prev.degraded_since || null) : null);
  let degradedAlerted = Boolean(prev.degraded_alerted);
  const healthyRun = degradedWindow.slice(-DEGRADED_CLEAR_CHECKS);
  const cleared = !degraded && !problems.length && healthyRun.length >= DEGRADED_CLEAR_CHECKS &&
    healthyRun.every((x) => x === 0);
  // Degraded turned down: the degraded incident is closed here (the down
  // alert below, escalated, carries it on).
  const turnedDown = degradedAlerted && problems.length > 0;
  if (degraded && !problems.length && !degradedAlerted && degradedWindow.length >= DEGRADED_WINDOW &&
      degradedCount >= DEGRADED_MIN) {
    const mins = Math.round((now - (degradedSince || now)) / 60000);
    degradedAlerted = await notify(env, "trigger", "Cavnar AI is degraded",
      `dashboard.cavnar.ai/health did not say "status":"ok" on ${degradedCount} of its last ${DEGRADED_WINDOW} ` +
      `checks (since ${mins} minutes ago). Its problems:\n` + (degraded.length ? degraded.join("\n") : "(none listed)") +
      "\n\nChecked from outside Railway by the cavnar-watchdog Worker: the in-app pages for these ride the " +
      "app's own email and text keys. You'll get one more message when it clears.", "cavnar-watchdog-degraded");
  } else if (degradedAlerted && (cleared || turnedDown)) {
    const ok = await notify(env, "resolve", turnedDown ? "Cavnar AI went from degraded to down" :
      "Cavnar AI is no longer degraded",
      turnedDown ? "The degraded alert is closed: the platform is now down (the down alert follows)." :
        'dashboard.cavnar.ai/health says "status":"ok" again.', "cavnar-watchdog-degraded");
    if (ok) {
      degradedAlerted = false;
      if (cleared) degradedSince = null;
    }                                    // else: tried again next check
  }
  if (!degraded && !degradedCount) degradedSince = null;
  const streak = problems.length ? (prev.streak || 0) + 1 : 0;
  let alerted = Boolean(prev.alerted);
  // A platform already alerted as degraded that goes down escalates on its
  // first down check; otherwise two checks in a row (a deploy's restart is
  // shorter than that).
  const needed = (turnedDown || prev.escalate) ? 1 : FAILS_BEFORE_ALERT;
  if (problems.length && streak >= needed && !alerted) {
    alerted = await notify(env, "trigger", "Cavnar AI is down", problems.join("\n") +
      "\n\nChecked from outside Railway by the cavnar-watchdog Worker. You'll get one more message when it recovers.");
  } else if (!problems.length && alerted) {
    if (await notify(env, "resolve", "Cavnar AI is back up", "The heartbeat and /health are both answering again.")) alerted = false;
  }
  // An escalation whose down alert did not go out is tried again next check.
  const escalate = Boolean((turnedDown || prev.escalate) && problems.length && !alerted);
  // Written only when it changed: a healthy hour is no writes at all.
  if (streak !== (prev.streak || 0) || alerted !== Boolean(prev.alerted) ||
      JSON.stringify(problems) !== JSON.stringify(prev.problems || []) ||
      (degradedSince || null) !== (prev.degraded_since || null) || degradedAlerted !== Boolean(prev.degraded_alerted) ||
      JSON.stringify(degradedWindow) !== JSON.stringify(windowBefore) || escalate !== Boolean(prev.escalate)) {
    await env.WATCH.put("state", JSON.stringify({ streak, alerted, checked_at: now, problems,
      degraded_since: degradedSince, degraded_alerted: degradedAlerted, degraded_window: degradedWindow,
      escalate }));
  }
}

function list(value) {
  return String(value || "").split(",").map((x) => x.trim()).filter(Boolean);
}

// Every configured channel, each on its own: one failing never stops the
// others. True when any of them accepted the alert.
async function notify(env, event, subject, text, dedup = "cavnar-watchdog-down") {
  const sends = [email(env, subject, text)];
  if (env.PAGERDUTY_ROUTING_KEY) sends.push(pagerduty(env, event, subject, dedup));
  if (env.PAGER_WEBHOOK_URL) sends.push(webhook(env, event, subject, text));
  if (env.TWILIO_ACCOUNT_SID && env.TWILIO_AUTH_TOKEN && env.TWILIO_FROM) {
    for (const to of list(env.ALERT_SMS_TO)) sends.push(sms(env, to, `${subject}: ${text}`.slice(0, 320)));
  }
  const results = await Promise.all(sends.map((p) => p.catch(() => false)));
  return results.some(Boolean);
}

async function email(env, subject, text) {
  const to = list(env.ALERT_TO);
  if (!env.RESEND_API_KEY || !to.length) return false;
  const r = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from: `Cavnar AI Watchdog <${env.ALERT_FROM}>`, to, subject, text }),
  });
  return r.ok;
}

async function pagerduty(env, event, subject, dedup) {
  const r = await fetch("https://events.pagerduty.com/v2/enqueue", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      routing_key: env.PAGERDUTY_ROUTING_KEY, event_action: event, dedup_key: dedup || "cavnar-watchdog-down",
      payload: { summary: subject, source: "cavnar-watchdog", severity: "critical" },
    }),
  });
  return r.ok;
}

async function webhook(env, event, subject, text) {
  const r = await fetch(env.PAGER_WEBHOOK_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event, subject, text, source: "cavnar-watchdog" }),
  });
  return r.ok;
}

async function sms(env, to, body) {
  const auth = btoa(`${env.TWILIO_ACCOUNT_SID}:${env.TWILIO_AUTH_TOKEN}`);
  const r = await fetch(`https://api.twilio.com/2010-04-01/Accounts/${env.TWILIO_ACCOUNT_SID}/Messages.json`, {
    method: "POST",
    headers: { Authorization: `Basic ${auth}`, "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ To: to, From: env.TWILIO_FROM, Body: body }).toString(),
  });
  return r.ok;
}
