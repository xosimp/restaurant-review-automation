"""
scripts/boot_check.py — boot the app the way a deploy does, twice on one
volume, and ask it for /health (audit #25 A05-01: the deploy gate's boot).

    python scripts/boot_check.py                          # a fresh SQLite volume
    python scripts/boot_check.py --database-url URL       # a SCRATCH Postgres
    python scripts/boot_check.py --times 3

Each boot is its own process importing hosted_dashboard — the whole module-
level boot production runs (require_volume, init_db and the init chain,
ensure_columns, the boot backfills, the job queue's start) — then GET
/health through the app's test client: 200 (ok or degraded) or the check
fails. The second boot meets every migration already applied, as the next
deploy does. No provider key reaches the child (never a .env, keys
stripped), the scheduler cannot start off Railway, and a Postgres URL is used
only with ALLOW_LOCAL_PG=1 set here deliberately — it must be a scratch
server (CI's service container), never production's.

Exit 0 when every boot answered 200; 1 otherwise, with the child's output.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CHILD = r"""
import os, sys, json, time
sys.path.insert(0, os.getcwd())
import dotenv
dotenv.load_dotenv = lambda *a, **k: False       # never the checkout's .env (production's keys)
t0 = time.monotonic()
import hosted_dashboard as h
boot_ms = int((time.monotonic() - t0) * 1000)
r = h.app.test_client().get("/health")
body = r.get_data(as_text=True)
print("BOOT", json.dumps({"status": r.status_code, "boot_ms": boot_ms, "health": body[:400]}))
sys.stdout.flush()
os._exit(0 if r.status_code == 200 else 1)
"""

_STRIP_SUFFIXES = ("_API_KEY", "_AUTH_TOKEN", "_SECRET_KEY", "_SECRET", "_TOKEN", "_DSN", "_PASSWORD")


def child_env(volume, database_url=None):
    """The environment a boot runs with: this one minus every credential and
    database URL, on `volume`, with Postgres only when asked."""
    env = {k: v for k, v in os.environ.items()
           if not k.endswith(_STRIP_SUFFIXES) and k not in ("DATABASE_URL", "SHADOW_DATABASE_URL", "ALLOW_LOCAL_PG")
           and not k.startswith("RAILWAY_")}
    env["RAILWAY_VOLUME_MOUNT_PATH"] = volume
    env["SECRET_KEY"] = "boot-check-not-a-secret"
    env.setdefault("CAVNAR_PIN_PEPPER", "boot-check-pepper")
    if database_url:
        env["DATABASE_URL"] = database_url
        env["ALLOW_LOCAL_PG"] = "1"
    return env


def boot_once(volume, database_url=None, timeout=300):
    started = time.monotonic()
    r = subprocess.run([sys.executable, "-c", _CHILD], cwd=ROOT, env=child_env(volume, database_url),
                       capture_output=True, text=True, timeout=timeout)
    line = next((x for x in r.stdout.splitlines() if x.startswith("BOOT ")), None)
    return {"ok": r.returncode == 0 and line is not None, "returncode": r.returncode,
            "seconds": round(time.monotonic() - started, 1), "line": line,
            "tail": (r.stdout[-1500:] + "\n" + r.stderr[-3000:]) if r.returncode or not line else ""}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--database-url", default=None, help="a SCRATCH Postgres (never production's)")
    ap.add_argument("--times", type=int, default=2)
    args = ap.parse_args(argv)
    volume = tempfile.mkdtemp(prefix="cavnar-boot-check-")
    open(os.path.join(volume, "reviews.db"), "a").close()
    where = "Postgres" if args.database_url else "SQLite"
    failed = False
    for i in range(1, args.times + 1):
        out = boot_once(volume, args.database_url)
        print(f"boot {i} on {where}: {'ok' if out['ok'] else 'FAILED'} in {out['seconds']}s  {out['line'] or ''}")
        if not out["ok"]:
            failed = True
            print(out["tail"])
            break
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
