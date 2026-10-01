// cavnar-watchdog — the monitor that lives OUTSIDE Railway.
//
// 1. Dead-man heartbeat: the scheduler GETs /ping/<token> at the end of every
//    tick (ops.ping_healthcheck, HEALTHCHECK_PING_URL); /ping/<token>/fail
//    marks a failed backup or digest. Silence past STALE_MINUTES pages Will.
// 2. Uptime: every 5 minutes the cron fetches /health and expects 200 with
//    "db":"ok". Two failing checks in a row page Will (a deploy's restart is
//    shorter than that); a recovery sends one "back up" email.
// Alerts go by Resend from the app's own sender. State lives in KV.
const HEALTH_URL = "https://dashboard.cavnar.ai/health";
const STALE_MINUTES = 20;
const FAILS_BEFORE_ALERT = 2;
// Workers KV's free tier allows 1,000 writes a day (10/1/26: the 50% alert).
// A heartbeat landed every scheduler tick and every in-job pulse (~80s), and
// the cron rewrote its state every 5 minutes - 600-1,000+ writes a day. Now
// a heartbeat is written at most once per PING_WRITE_MINUTES (a failure at
// once), and the state only when it changes: about 300 writes a day.
const PING_WRITE_MINUTES = 4;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const m = url.pathname.match(/^\/ping\/([A-Za-z0-9_-]+)(\/fail)?\/?$/);
    if (!m || m[1] !== env.PING_TOKEN) return new Response("not found", { status: 404 });
    const fail = Boolean(m[2]);
    const last = JSON.parse((await env.WATCH.get("ping")) || "null");
    // Reads are cheap (100,000 a day); a write only when the news is new: a
    // failure, a recovery from one, or a heartbeat older than the window.
    if (!last || fail || last.fail || Date.now() - last.at >= PING_WRITE_MINUTES * 60000) {
      await env.WATCH.put("ping", JSON.stringify({ at: Date.now(), fail }));
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
  if (ping && ping.fail) problems.push("The scheduler reported a failure (the nightly backup or the digest).");
  else if (ping && now - ping.at > STALE_MINUTES * 60000)
    problems.push(`No heartbeat from the scheduler for ${Math.round((now - ping.at) / 60000)} minutes — its jobs have stopped.`);
  try {
    const r = await fetch(HEALTH_URL, { headers: { "User-Agent": "cavnar-watchdog" }, cf: { cacheTtl: 0 } });
    if (r.status !== 200) problems.push(`dashboard.cavnar.ai/health answered ${r.status}.`);
    else {
      const j = await r.json().catch(() => null);
      if (!j || j.db !== "ok") problems.push(`dashboard.cavnar.ai/health says the database is ${j ? j.db : "unreadable"}.`);
    }
  } catch (e) {
    problems.push(`dashboard.cavnar.ai/health could not be reached (${e.message}).`);
  }
  const prev = JSON.parse((await env.WATCH.get("state")) || '{"streak":0,"alerted":false}');
  const streak = problems.length ? (prev.streak || 0) + 1 : 0;
  let alerted = Boolean(prev.alerted);
  if (problems.length && streak >= FAILS_BEFORE_ALERT && !alerted) {
    alerted = await email(env, "Cavnar AI is down", problems.join("\n") +
      "\n\nChecked from outside Railway by the cavnar-watchdog Worker. You'll get one more email when it recovers.");
  } else if (!problems.length && alerted) {
    if (await email(env, "Cavnar AI is back up", "The heartbeat and /health are both answering again.")) alerted = false;
  }
  // Written only when it changed: a healthy hour is no writes at all.
  if (streak !== (prev.streak || 0) || alerted !== Boolean(prev.alerted) ||
      JSON.stringify(problems) !== JSON.stringify(prev.problems || [])) {
    await env.WATCH.put("state", JSON.stringify({ streak, alerted, checked_at: now, problems }));
  }
}

async function email(env, subject, text) {
  const r = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from: `Cavnar AI Watchdog <${env.ALERT_FROM}>`, to: [env.ALERT_TO], subject, text }),
  });
  return r.ok;
}
