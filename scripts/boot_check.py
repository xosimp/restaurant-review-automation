"""
scripts/boot_check.py — boot the app the way a deploy does, twice on one
volume, and ask it for /health (audit #25 A05-01: the deploy gate's boot;
blind re-audit 10/27/26 B1 F7, B5 "the gate never boots in production mode").

    python scripts/boot_check.py                          # a fresh SQLite volume
    python scripts/boot_check.py --database-url URL       # a SCRATCH Postgres
    python scripts/boot_check.py --database-url URL --partitioned
    python scripts/boot_check.py --production [--database-url URL]
    python scripts/boot_check.py --gunicorn [--database-url URL]
    python scripts/boot_check.py --times 3

Each boot is its own process importing hosted_dashboard — the whole module-
level boot production runs (require_volume, init_db and the init chain,
ensure_columns, the boot backfills, the job queue's start) — then GET
/health through the app's test client: 200 (ok or degraded) or the check
fails. The second boot meets every migration already applied, as the next
deploy does.

--partitioned builds the Postgres database the way production's was before
the boots: the boot schema on SQLite, then pg_copy (the fact tables in
db_dialect.PARTITIONED month-partitioned, their keys carrying the partition
column) — a boot DDL that is fine on a plain table and refused on a
partitioned parent fails here, not in production.

--production boots as Railway does: the Railway markers set and
RAILWAY_ENVIRONMENT_NAME=production, so scheduling_allowed() is true and the
production-only boot runs — the scheduler thread, its lease keeper and the
supervisor — and the check waits (up to --tick-seconds) for the scheduler to
COMPLETE a tick (scheduler_heartbeat.loop_completed_at), its thread still
alive. With no provider key in the environment and no connection off the
machine, every send and every provider call fails as it would offline.

--gunicorn starts gunicorn exactly as railway.json does (gunicorn.conf.py's
master, one web worker, `worker.py --queue` beside it) on 127.0.0.1 with the
production markers, polls /health for Railway's 120 seconds, waits for the
queue worker's heartbeat (job_workers), then SIGTERMs the master and expects
it to exit cleanly inside the drain budget.

Every child runs with an ALLOWLISTED environment — never the checkout's
.env, no credential, no database URL but the one given — and with the
network guard (scripts/boot_guard/sitecustomize.py): a connection to
anything but this machine or the scratch Postgres is refused and listed, and
fails the check. A Postgres URL must be a scratch server (CI's service
container), never production's.

Exit 0 when every boot answered 200; 1 otherwise, with the child's output.
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD_DIR = os.path.join(ROOT, "scripts", "boot_guard")
HEALTH_SECONDS = 120            # Railway's healthcheckTimeout (railway.json)
TICK_SECONDS = 180

_CHILD = r"""
import os, sys, json, time
sys.path.insert(0, os.getcwd())
import dotenv
dotenv.load_dotenv = lambda *a, **k: False       # never the checkout's .env (production's keys)
before = None
if os.environ.get("BOOT_CHECK_TICK"):
    # The tick an earlier boot on this volume completed is not this boot's.
    try:
        import models
        conn = models.get_conn()
        try:
            row = conn.execute("SELECT loop_completed_at FROM scheduler_heartbeat WHERE id=1").fetchone()
            before = row["loop_completed_at"] if row else None
        finally:
            conn.close()
    except Exception:
        before = None
t0 = time.monotonic()
import hosted_dashboard as h
boot_ms = int((time.monotonic() - t0) * 1000)
r = h.app.test_client().get("/health")
body = r.get_data(as_text=True)
import config
out = {"status": r.status_code, "boot_ms": boot_ms, "env": config.deploy_env(), "health": body[:400]}
ok = r.status_code == 200
if os.environ.get("BOOT_CHECK_TICK") and ok:
    # The production boot: the scheduler must be running and complete a tick.
    deadline = time.monotonic() + float(os.environ["BOOT_CHECK_TICK"])
    thread = getattr(h, "_SCHED_THREAD", None)
    out["scheduler_thread"] = getattr(thread, "name", None)
    tick = None
    import models
    while time.monotonic() < deadline:
        if thread is None or not thread.is_alive():
            break
        conn = models.get_conn()
        try:
            row = conn.execute("SELECT loop_completed_at FROM scheduler_heartbeat WHERE id=1").fetchone()
        finally:
            conn.close()
        tick = row["loop_completed_at"] if row else None
        if tick and tick != before:
            break
        tick = None
        time.sleep(1.0)
    out["tick_completed_at"] = tick
    out["scheduler_alive"] = bool(thread is not None and thread.is_alive())
    ok = bool(tick) and out["scheduler_alive"]
    # What a redeploy's clean exit does (scheduler.register_shutdown): give
    # the lease up, so the next boot's scheduler takes it on its first tick.
    import ops
    ops.shutdown_scheduler()
# A file, not stdout: on Railway markers the app's structured logging wraps
# print() into a JSON line.
with open(os.environ["BOOT_CHECK_OUT"], "w") as fh:
    fh.write(json.dumps(out, default=str))
os._exit(0 if ok else 1)
"""

# Variables a boot child may inherit; everything else (every key, token,
# database URL, Railway marker, Flask/dotenv setting) is left behind.
_PASS = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT",
         "PYTHONHASHSEED", "CI", "GITHUB_ACTIONS")
# What the production markers look like on Railway (config.RAILWAY_MARKERS).
PRODUCTION_MARKERS = {"RAILWAY_ENVIRONMENT_NAME": "production", "RAILWAY_ENVIRONMENT": "production",
                      "RAILWAY_PROJECT_ID": "boot-check-project", "RAILWAY_SERVICE_ID": "boot-check-web",
                      "RAILWAY_DEPLOYMENT_ID": "boot-check-deployment"}


def child_env(volume, database_url=None, production=False, guard_log=None):
    """The environment a boot runs with: an allowlist of this one (no
    credential, no database URL, no Railway marker), on `volume`, with
    Postgres only when asked, the production markers when asked, and the
    network guard on."""
    env = {k: v for k, v in os.environ.items() if k in _PASS}
    env["RAILWAY_VOLUME_MOUNT_PATH"] = volume
    env["SECRET_KEY"] = "boot-check-not-a-secret"
    env["CAVNAR_PIN_PEPPER"] = "boot-check-pepper"
    env["PYTHONPATH"] = GUARD_DIR
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CAVNAR_NET_GUARD"] = "1"
    if guard_log:
        env["CAVNAR_NET_GUARD_LOG"] = guard_log
    if database_url:
        env["DATABASE_URL"] = database_url
        env["CAVNAR_NET_GUARD_ALLOW"] = (urlsplit(database_url).hostname or "").lower()
        if not production:
            env["ALLOW_LOCAL_PG"] = "1"      # off Railway a Postgres URL is used only when said so
    if production:
        env.update(PRODUCTION_MARKERS)
    return env


def _blocked(guard_log):
    if not guard_log or not os.path.exists(guard_log):
        return []
    with open(guard_log, encoding="utf-8") as fh:
        return sorted({line.split(" ", 1)[-1].strip() for line in fh if line.strip()})


def boot_once(volume, database_url=None, production=False, tick_seconds=TICK_SECONDS, timeout=420):
    guard_log = os.path.join(volume, f"blocked-{time.monotonic_ns()}.log")
    env = child_env(volume, database_url, production=production, guard_log=guard_log)
    if production:
        env["BOOT_CHECK_TICK"] = str(tick_seconds)
    result_path = os.path.join(volume, f"boot-{time.monotonic_ns()}.json")
    env["BOOT_CHECK_OUT"] = result_path
    started = time.monotonic()
    r = subprocess.run([sys.executable, "-c", _CHILD], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=timeout)
    line = None
    if os.path.exists(result_path):
        with open(result_path, encoding="utf-8") as fh:
            line = fh.read()
    blocked = _blocked(guard_log)
    ok = r.returncode == 0 and line is not None and not blocked
    return {"ok": ok, "returncode": r.returncode, "seconds": round(time.monotonic() - started, 1), "line": line,
            "blocked": blocked, "tail": (r.stdout[-1500:] + "\n" + r.stderr[-3000:]) if not ok else ""}


def summary(line):
    """The child's answer in one short line: /health's status and env, the
    boot time, and (production) the scheduler's tick."""
    import json
    try:
        got = json.loads(line or "")
    except ValueError:
        return line or ""
    try:
        health = json.loads(got.get("health") or "{}")
    except ValueError:
        health = {}
    keep = {k: got[k] for k in ("status", "env", "boot_ms", "scheduler_thread", "scheduler_alive",
                                "tick_completed_at") if k in got}
    if isinstance(health, dict) and health.get("status"):
        keep["health"] = health.get("status")
    return json.dumps(keep)


def build_partitioned(volume, database_url):
    """Production's database shape on the scratch server: the boot schema on
    SQLite in `volume`, then pg_copy into `database_url` (partitioned fact
    tables), as production's database was made before its first boot there."""
    code = (
        "import os, sys, json\n"
        "sys.path.insert(0, os.getcwd())\n"
        "import dotenv\n"
        "dotenv.load_dotenv = lambda *a, **k: False\n"
        "import models, psycopg, pg_copy\n"
        "models.boot_schema()\n"
        "with psycopg.connect(sys.argv[1], connect_timeout=10, autocommit=True) as dst:\n"
        "    rep = pg_copy.copy_database(models.DB_PATH, dst)\n"
        "    parts = dst.execute('SELECT count(*) FROM pg_partitioned_table').fetchone()[0]\n"
        "bad = [r for r in rep['foreign_keys']['refused'] if 'does not exist' not in r['error']]\n"
        "print('COPY', json.dumps({'partitioned_tables': parts, 'refused': bad[:3]}, default=str))\n"
        "sys.exit(1 if bad or not parts else 0)\n")
    env = child_env(volume)               # SQLite only: no DATABASE_URL in the child
    r = subprocess.run([sys.executable, "-c", code, database_url], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=600)
    line = next((x for x in r.stdout.splitlines() if x.startswith("COPY ")), "")
    return r.returncode == 0, line or (r.stdout[-1500:] + "\n" + r.stderr[-3000:])


# ── gunicorn, as railway.json starts it ───────────────────────────────────────

def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def gunicorn_command(port):
    """railway.json's startCommand, bound to loopback."""
    return [sys.executable, "-m", "gunicorn", "hosted_dashboard:app", "--bind", f"127.0.0.1:{port}",
            "--workers", "1", "--threads", "4", "--timeout", "120", "--keep-alive", "5", "--log-level", "info"]


def wait_for(probe, seconds, every=1.0, clock=time.monotonic, sleep=time.sleep):
    """probe() until it returns something truthy (returned) or `seconds`
    pass (None)."""
    deadline = clock() + seconds
    while clock() < deadline:
        got = probe()
        if got:
            return got
        sleep(every)
    return None


def _health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _queue_worker_beat(env):
    """Whether a `worker.py --queue` heartbeat is fresh in the boot's
    database (read in a child with the boot's own environment)."""
    code = ("import os, sys\nsys.path.insert(0, os.getcwd())\nimport dotenv\n"
            "dotenv.load_dotenv = lambda *a, **k: False\nimport job_queue\n"
            "sys.exit(0 if job_queue.worker_alive() else 1)\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def gunicorn_boot(volume, database_url=None, health_seconds=HEALTH_SECONDS, stop_seconds=90):
    guard_log = os.path.join(volume, "blocked-gunicorn.log")
    env = child_env(volume, database_url, production=True, guard_log=guard_log)
    port = free_port()
    env["PORT"] = str(port)
    started = time.monotonic()
    out_path = os.path.join(volume, "gunicorn.log")
    out = open(out_path, "w", encoding="utf-8")
    proc = subprocess.Popen(gunicorn_command(port), cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True)
    result = {"ok": False, "health": False, "queue_worker": False, "exit": None}
    try:
        result["health"] = bool(wait_for(lambda: proc.poll() is None and _health(port), health_seconds))
        if result["health"]:
            result["queue_worker"] = bool(wait_for(lambda: _queue_worker_beat(env), 60, every=2.0))
        proc.send_signal(signal.SIGTERM)
        try:
            result["exit"] = proc.wait(timeout=stop_seconds)
        except subprocess.TimeoutExpired:
            result["exit"] = "did not stop"
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)
        out.close()
    result["blocked"] = _blocked(guard_log)
    result["seconds"] = round(time.monotonic() - started, 1)
    result["ok"] = result["health"] and result["queue_worker"] and result["exit"] == 0 and not result["blocked"]
    if not result["ok"]:
        with open(out_path, encoding="utf-8", errors="replace") as fh:
            result["tail"] = fh.read()[-4000:]
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--database-url", default=None, help="a SCRATCH Postgres (never production's)")
    ap.add_argument("--partitioned", action="store_true",
                    help="build the Postgres database with pg_copy first (production's partitioned shape)")
    ap.add_argument("--production", action="store_true",
                    help="boot with the Railway markers (production): the scheduler starts and must complete a tick")
    ap.add_argument("--gunicorn", action="store_true", help="gunicorn as railway.json starts it, then SIGTERM")
    ap.add_argument("--times", type=int, default=2)
    ap.add_argument("--tick-seconds", type=float, default=TICK_SECONDS)
    args = ap.parse_args(argv)
    if args.partitioned and not args.database_url:
        ap.error("--partitioned needs --database-url")
    volume = tempfile.mkdtemp(prefix="cavnar-boot-check-")
    open(os.path.join(volume, "reviews.db"), "a").close()
    where = ("Postgres" + (" (partitioned by pg_copy)" if args.partitioned else "")) if args.database_url else "SQLite"
    if args.partitioned:
        ok, line = build_partitioned(tempfile.mkdtemp(prefix="cavnar-boot-copy-"), args.database_url)
        print(f"pg_copy into the boot database: {'ok' if ok else 'FAILED'}  {line}")
        if not ok:
            return 1
    if args.gunicorn:
        res = gunicorn_boot(volume, args.database_url)
        print(f"gunicorn on {where}, production markers: {'ok' if res['ok'] else 'FAILED'} in {res['seconds']}s  "
              f"health={res['health']} queue_worker={res['queue_worker']} exit={res['exit']} "
              f"blocked={res['blocked']}")
        if not res["ok"]:
            print(res.get("tail", ""))
        return 0 if res["ok"] else 1
    mode = "production markers" if args.production else "local"
    failed = False
    for i in range(1, args.times + 1):
        out = boot_once(volume, args.database_url, production=args.production, tick_seconds=args.tick_seconds)
        print(f"boot {i} on {where} ({mode}): {'ok' if out['ok'] else 'FAILED'} in {out['seconds']}s  "
              f"{summary(out['line'])}")
        if out["blocked"]:
            print(f"  connections the boot tried off this machine (refused): {out['blocked']}")
        if not out["ok"]:
            failed = True
            print(out["tail"])
            break
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
