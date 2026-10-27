"""The real composed app, for permission and tenant tests (audit #25 A01-15).

Most route tests build `Flask(__name__)` with the blueprints they need and
fake the user (`auth.get_current_user` patched, a session cookie forged). The
app-level hooks — CSRF on the protected blueprints, the staff portal's CSRF
check, support masking on every /admin and view-as response, the CSRF
cookie — and the real session resolution (`get_session_user`: membership,
role, view-as) exist only in the composed app, so a regression in one of them
passed all 1,800 of those tests.

`RealApp` boots `hosted_dashboard` in a child process (a throwaway volume, no
provider keys, no .env, the network blocked, no scheduler — the way
tests/test_ios_request_body_parity.py and the cross-tenant crawl boot it) and
drives it over a line-based pipe: no port, no server. A `RealClient` signs in
through the REAL door for its role and keeps its own cookie jar:

- "owner" / "manager" / "member" / "admin" / "support": the /login form
  (its csrf_token), so the session, the CSRF cookie and the role are the
  app's own; a web client's writes echo the csrf_js cookie in X-CSRF, as the
  dashboard's fetch wrapper does (pass `csrf=False` to leave it off);
- "mobile:<role>": /mobile/api/login, then `Authorization: Bearer`;
- "staff": the join code, the roster's nonce and the PIN (/staff/r/<code>/login);
- `support.view_as(rid, reason)`: the console's view-as POST — the session
  then reads as the owner, through support masking.

Usage (module-scoped: one boot per test file, ~5 s):

    from real_app import real_app          # the fixture
    def test_x(real_app):
        rid = real_app.restaurant(name="R", module_labor=1)
        owner = real_app.client("owner", rid)
        assert owner.get("/api/account/team").status == 200

`real_app.call("module.function", *args, **kwargs)` runs app code in the
child (seeding, reading back) with JSON arguments and result;
`real_app.query("SELECT …", *params)` reads its database.
"""
import json
import os
import select
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TIMEOUT = 120


class RealAppError(RuntimeError):
    pass


class Resp:
    def __init__(self, d):
        self.status = d["status"]
        self.headers = d["headers"]
        self.text = d["text"]
        self.json = d.get("json")

    def __repr__(self):
        return f"<Resp {self.status} {self.text[:120]!r}>"


class RealClient:
    def __init__(self, app, cid, kind, user_id):
        self._app, self.cid, self.kind, self.user_id = app, cid, kind, user_id

    def request(self, method, path, json_body=None, data=None, headers=None, csrf=True):
        return Resp(self._app._send({"op": "request", "client": self.cid, "method": method, "path": path,
                                     "json": json_body, "data": data, "headers": headers or {}, "csrf": csrf}))

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json_body=json if json is not None else ({} if "data" not in kw else None),
                            **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def view_as(self, restaurant_id, reason="checking a ticket for them"):
        """A support or admin login opens view-as on `restaurant_id` (the
        console's POST, a reason, CSRF)."""
        r = self.request("POST", f"/admin/view-as/{int(restaurant_id)}", data={"reason": reason})
        if r.status != 302:
            raise RealAppError(f"view-as refused: {r}")
        return r


class RealApp:
    def __init__(self):
        self._err = tempfile.NamedTemporaryFile(prefix="realapp-", suffix=".log", delete=False)
        env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN", "_SECRET"))}
        env.pop("DATABASE_URL", None)
        self._proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve"], cwd=ROOT, env=env,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._err, text=True,
                                      bufsize=1)
        self._n = 0
        hello = self._read()
        if not hello.get("ready"):
            raise RealAppError(f"the app did not boot: {hello}")

    def _read(self):
        ready, _, _ = select.select([self._proc.stdout], [], [], _TIMEOUT)
        line = self._proc.stdout.readline() if ready else ""
        if not line:
            self._err.flush()
            with open(self._err.name, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-4000:]
            raise RealAppError(f"the app process stopped answering:\n{tail}")
        return json.loads(line)

    def _send(self, msg):
        self._n += 1
        msg["id"] = self._n
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()
        out = self._read()
        if out.get("error"):
            raise RealAppError(out["error"])
        return out["result"]

    def call(self, fn, *args, **kwargs):
        return self._send({"op": "call", "fn": fn, "args": list(args), "kwargs": kwargs})

    def query(self, sql, *params):
        """Rows of a read in the child's database, as dicts (a SELECT only)."""
        return self._send({"op": "query", "sql": sql, "params": list(params)})

    def restaurant(self, **fields):
        fields.setdefault("name", "Real App Co")
        fields.setdefault("owner_email", f"owner-{self._n}@realapp.test")
        return self._send({"op": "restaurant", "fields": fields})

    def client(self, role, restaurant_id=None, **kw):
        """A client signed in through the real door for `role` (see the
        module docstring). Creates the login."""
        cid, uid = self._send({"op": "login", "role": role, "restaurant_id": restaurant_id, "kw": kw})
        return RealClient(self, cid, role, uid)

    def close(self):
        try:
            self._proc.stdin.write(json.dumps({"op": "quit", "id": 0}) + "\n")
            self._proc.stdin.flush()
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()
            self._proc.wait(timeout=10)
        finally:
            self._err.close()
            os.unlink(self._err.name)


@pytest.fixture(scope="module")
def real_app():
    app = RealApp()
    try:
        yield app
    finally:
        app.close()


# ═════════════════════════════════════════════════════════════════════════════
# The child process
# ═════════════════════════════════════════════════════════════════════════════

def _serve():
    import contextlib
    import importlib
    import io
    import re
    import socket
    import sqlite3

    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)                                   # anything the app prints goes to the log, not the pipe
    sys.stdout = sys.stderr
    vol = tempfile.mkdtemp(prefix="cavnar-realapp-")
    sqlite3.connect(os.path.join(vol, "reviews.db")).close()
    for k in list(os.environ):
        if k.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_AUTH_TOKEN", "_KEY", "_PASSWORD", "_DSN")) or \
                k in ("DATABASE_URL", "CAVNAR_TEST_PG_URL", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID"):
            os.environ.pop(k)
    os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", SECRET_KEY="test-secret",
                      CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0",
                      REQUEST_LIMITS="0", AI_SHADOW_REVIEW="0", HOME_SNAPSHOT_COLD="request",
                      CAVNAR_POSTAL_ADDRESS="100 Test St, Testville, IL 60000")

    def _blocked(*a, **k):
        raise OSError("network blocked in the real-app test process")
    socket.create_connection = _blocked
    socket.socket.connect = lambda self, *a, **k: _blocked()
    sys.path.insert(0, ROOT)
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    with contextlib.redirect_stdout(io.StringIO()):
        import hosted_dashboard
    import auth
    import models
    app = hosted_dashboard.app
    app.config["PROPAGATE_EXCEPTIONS"] = False
    auth.ENFORCE_PASSWORD_POLICY = False
    clients = {}
    seq = [0]

    def web_login(username, password):
        c = app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"username": username, "password": password, "csrf_token": token})
        if r.status_code not in (302, 303):
            raise RuntimeError(f"sign-in refused for {username}: {r.status_code} {r.get_data()[:200]!r}")
        return c

    def login(role, restaurant_id, kw):
        seq[0] += 1
        n = seq[0]
        password = f"real-app-pass-{n}"
        mobile = role.startswith("mobile:")
        base = role.split(":", 1)[1] if mobile else role
        if base in ("admin", "support"):
            hq = models.create_restaurant(models.Restaurant(name=f"Cavnar AI HQ {n}", owner_email=f"hq{n}@cavnar.test"))
            uid = auth.create_user(hq, f"{base}{n}", f"{base}{n}@cavnar.test", password, is_admin=(base == "admin"))
            if base == "support":
                auth.set_user_role(uid, "support")
        elif base == "staff":
            uid = auth.create_user(restaurant_id, f"emp{n}", f"emp{n}@staff.invalid", "x" * 24)
            m = auth.upsert_membership(uid, restaurant_id, "employee", employee_name=kw.get("name") or f"Emp {n}",
                                       job_role=kw.get("job_role"))
            auth.set_membership_pin(m["id"], restaurant_id, "8317")
            c = app.test_client()
            code = auth.get_join_code(restaurant_id)
            roster = c.get(f"/staff/api/roster/{code}").get_json()
            r = c.post(f"/staff/r/{code}/login", json={"membership_id": m["id"], "pin": "8317",
                                                       "nonce": roster["login_nonce"]})
            if r.status_code != 200:
                raise RuntimeError(f"staff sign-in refused: {r.status_code} {r.get_data()[:200]!r}")
            clients[n] = {"c": c, "kind": "staff", "bearer": None}
            return [n, uid]
        else:
            # "owner" is the restaurant's primary login (role 'client'); with
            # multi=True it is a multi-location owner (role 'owner').
            stored = {"owner": "owner" if kw.get("multi") else "client", "manager": "manager",
                      "member": "member"}.get(base)
            if stored is None:
                raise RuntimeError(f"unknown role {role!r}")
            uid = auth.create_user(restaurant_id, f"{base}{n}", f"{base}{n}@realapp.test", password)
            auth.set_user_role(uid, stored)
            auth.upsert_membership(uid, restaurant_id, stored)
        if mobile:
            c = app.test_client()
            r = c.post("/mobile/api/login", json={"username": f"{base}{n}", "password": password}).get_json() or {}
            if not r.get("token"):
                raise RuntimeError(f"mobile sign-in refused: {r}")
            clients[n] = {"c": c, "kind": "mobile", "bearer": r["token"]}
        else:
            clients[n] = {"c": web_login(f"{base}{n}", password), "kind": "web", "bearer": None}
        return [n, uid]

    def do_request(msg):
        ent = clients[msg["client"]]
        c, headers = ent["c"], dict(msg.get("headers") or {})
        if ent["bearer"]:
            headers.setdefault("Authorization", f"Bearer {ent['bearer']}")
        if msg.get("csrf", True) and msg["method"] not in ("GET", "HEAD", "OPTIONS"):
            ck = c.get_cookie("csrf_js")
            if ck is not None:
                headers.setdefault("X-CSRF", ck.value)
        kw = {"headers": headers}
        if msg.get("json") is not None:
            kw["json"] = msg["json"]
        elif msg.get("data") is not None:
            kw["data"] = msg["data"]
        r = c.open(msg["path"], method=msg["method"], **kw)
        text = r.get_data(as_text=True)
        body = r.get_json(silent=True)          # None for a body that is not JSON
        return {"status": r.status_code, "headers": dict(r.headers), "text": text, "json": body}

    def do_call(msg):
        mod_name, _, attr = msg["fn"].rpartition(".")
        fn = getattr(importlib.import_module(mod_name), attr)
        out = fn(*msg.get("args") or [], **(msg.get("kwargs") or {}))
        try:
            json.dumps(out)
            return out
        except TypeError:
            if hasattr(out, "__dataclass_fields__"):
                import dataclasses
                return json.loads(json.dumps(dataclasses.asdict(out), default=str))
            if isinstance(out, (list, tuple)):
                return json.loads(json.dumps([dict(x) if hasattr(x, "keys") else x for x in out], default=str))
            if hasattr(out, "keys"):
                return json.loads(json.dumps(dict(out), default=str))
            return repr(out)

    proto.write(json.dumps({"ready": True}) + "\n")
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        msg = json.loads(line)
        if msg.get("op") == "quit":
            break
        try:
            if msg["op"] == "request":
                result = do_request(msg)
            elif msg["op"] == "call":
                result = do_call(msg)
            elif msg["op"] == "query":
                if not msg["sql"].lstrip().upper().startswith("SELECT"):
                    raise RuntimeError("query runs a SELECT only")
                conn = models.get_conn()
                try:
                    result = [dict(r) for r in conn.execute(msg["sql"], tuple(msg.get("params") or ())).fetchall()]
                finally:
                    conn.close()
            elif msg["op"] == "restaurant":
                fields = dict(msg["fields"])
                fields.setdefault("service_tier", "full")
                fields.setdefault("billing_status", "active")
                result = models.create_restaurant(models.Restaurant(**fields))
            elif msg["op"] == "login":
                result = login(msg["role"], msg.get("restaurant_id"), msg.get("kw") or {})
            else:
                raise RuntimeError(f"unknown op {msg.get('op')!r}")
            proto.write(json.dumps({"id": msg.get("id"), "result": result}, default=str) + "\n")
        except Exception as e:
            import traceback
            proto.write(json.dumps({"id": msg.get("id"), "error": f"{type(e).__name__}: {e}\n"
                                                                   f"{traceback.format_exc()[-1500:]}"}) + "\n")
    import shutil
    shutil.rmtree(vol, ignore_errors=True)


if __name__ == "__main__" and sys.argv[1:] == ["--serve"]:
    _serve()
    os._exit(0)
