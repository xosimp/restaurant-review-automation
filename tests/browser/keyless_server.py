"""A throwaway, keyless Cavnar AI web app for the browser smoke
(tests/browser/test_dashboard_smoke.py starts it, and stops it when done).

    python tests/browser/keyless_server.py <volume dir> <info file>

It is never the developer's app and never production's:

- The volume is the caller's fresh temporary directory, with an empty
  reviews.db created first (models.adopt_legacy_db would otherwise copy a
  ./reviews.db into it).
- No provider keys. The caller builds this process's environment from an
  allowlist (PATH, HOME, …) plus the test settings; this process refuses to
  start if any key-, token-, secret- or database-URL-shaped variable is set
  anyway, before AND after importing the app. dotenv.load_dotenv is a no-op
  before anything imports it: hosted_dashboard loads the .env beside it, and
  in the main checkout that is production's (CLAUDE.md, the worktree .env
  hazard). Flask's own .env lookup never runs — the app is served by
  werkzeug's make_server, not app.run — and FLASK_SKIP_DOTENV=1 besides.
- No network beyond loopback: socket connects and name lookups to any other
  host are refused and written to <volume>/network_attempts.log, which the
  smoke asserts is empty.
- No scheduler (RUN_SCHEDULER_IN_WEB=0, and scheduling_allowed() is false
  off Railway), so no brief, digest or alert can be sent.
- Bound to 127.0.0.1 on a free port chosen by the OS (never 5050).
- Never outlives its caller: it exits when its parent process is gone, or
  after SMOKE_SERVER_MAX_SECONDS (default 900) whatever happens.

The demo account (demo_seed's placeholder bar and grill, every figure
synthetic) is seeded synchronously, with DEMO_PASSWORD from the caller, and
given a synthetic Google place id so the Intel tab is on the page. The info
file gets {"port", "username", "restaurant_id"} once the server listens.
"""
import json
import os
import re
import signal
import socket
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set deliberately by the caller, and not a provider's.
_OURS = {"SECRET_KEY", "DEMO_PASSWORD", "CAVNAR_PIN_PEPPER"}
_SECRET_SHAPED = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|DSN|CREDENTIAL|DATABASE_URL|PG_URL|AUTH)",
                            re.I)
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def secret_shaped_vars(environ):
    return sorted(k for k, v in environ.items() if v and k not in _OURS and _SECRET_SHAPED.search(k))


def _refuse_if_keyed(when):
    found = secret_shaped_vars(os.environ)
    if found:
        sys.stderr.write("keyless_server: refusing to run %s with %s set\n" % (when, ", ".join(found)))
        os._exit(3)


def _guard_network(log_path):
    """Loopback only. A refused attempt is logged (host only) and raised."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def note(host):
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("%s\n" % (host,))

    def check(address):
        if isinstance(address, tuple) and address and str(address[0]) not in _LOOPBACK:
            note(address[0])
            raise OSError("keyless smoke app: no network (%s)" % (address[0],))

    def connect(self, address):
        check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *a, **k):
        if host is not None and str(host) not in _LOOPBACK:
            note(host)
            raise socket.gaierror(socket.EAI_NONAME, "keyless smoke app: no network (%s)" % (host,))
        return real_getaddrinfo(host, *a, **k)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo


def _exit_with_parent(max_seconds):
    """A daemon thread: exit when the caller is gone (this process is
    re-parented) or the lifetime is spent, so a killed test run leaves no
    server behind."""
    import threading
    import time
    parent, started = os.getppid(), time.monotonic()

    def watch():
        while True:
            time.sleep(1)
            if os.getppid() != parent or time.monotonic() - started > max_seconds:
                os._exit(0)
    threading.Thread(target=watch, daemon=True, name="smoke-server-watchdog").start()


def main(vol, info_path):
    _exit_with_parent(float(os.environ.get("SMOKE_SERVER_MAX_SECONDS") or 900))
    _refuse_if_keyed("before boot")
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    sqlite3.connect(os.path.join(vol, "reviews.db")).close()
    os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = vol
    os.environ["RUN_SCHEDULER_IN_WEB"] = "0"
    os.environ["FLASK_SKIP_DOTENV"] = "1"
    _guard_network(os.path.join(vol, "network_attempts.log"))

    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    import demo_seed
    demo_seed.start_background_seed = lambda: None      # seeded below, before the port is published

    import hosted_dashboard
    _refuse_if_keyed("after boot")

    import models
    rid = demo_seed._seed_simple_ejs()
    models.update_restaurant(rid, {"google_place_id": "ChIJ-smoke-test-not-a-place"})

    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, hosted_dashboard.app, threaded=True)
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    tmp = info_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"port": server.server_port, "username": demo_seed.SIMPLE_EJS_USERNAME,
                   "restaurant_id": rid}, fh)
    os.replace(tmp, info_path)
    server.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
