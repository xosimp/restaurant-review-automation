"""
smoke.py — the post-deploy smoke test (release-safety audit 10/26/26,
A05-11 / A07-01 / R15).

/health proves the database and the schema, not the app. After every
production deploy (.github/workflows/release.yml, on Railway's
deployment_status success) this checks, against the live site:

  1. /health answers 200 and its `build` is the SHA that was deployed (never
     "unknown": a `railway up` deploy names no commit — A05-13);
  2. /login renders (200, the sign-in form);
  3. the static files /login references resolve (200) — a broken bundle or
     a missing file is a blank page for every owner;
  4. with SMOKE_TOKEN (a session token of the smoke tenant's login — a
     GitHub secret, never a real restaurant's), one authenticated web read
     (/api/home/brief) and one mobile read (/mobile/api/me) answer 200 JSON.

Exit 0 when every check passed; 1 and the Railway rollback step otherwise.

    python scripts/smoke.py https://dashboard.cavnar.ai --expect-sha "$GITHUB_SHA"
    SMOKE_TOKEN=… python scripts/smoke.py https://dashboard.cavnar.ai --expect-sha abc123

Reads only; writes and sends nothing.
"""
import argparse
import json
import os
import re
import sys
import time

TIMEOUT = 15            # seconds per request (every outbound call names one)
HEALTH_RETRIES = 6      # /health right after a deploy can still be the old container
HEALTH_RETRY_SECONDS = 10
ROLLBACK_HINT = ("Roll back: Railway → web service → Deployments → the previous deployment → Redeploy "
                 "(or `railway redeploy` of that deployment). docs/ops/RECOVERY.md → Rolling back a release.")

_ASSET = re.compile(r'(?:src|href)="(/(?:static|b)/[^"#]+)"')


def http_fetch(base_url):
    """fetch(path, headers) -> (status, headers, body) over HTTPS."""
    import requests

    def fetch(path, headers=None):
        r = requests.get(base_url.rstrip("/") + path, headers=headers or {}, timeout=TIMEOUT,
                         allow_redirects=False)
        return r.status_code, dict(r.headers), r.content
    return fetch


def _json(body):
    try:
        return json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def check_health(fetch, expect_sha=None, retries=HEALTH_RETRIES, pause=HEALTH_RETRY_SECONDS, sleep=time.sleep,
                 expect_env=None):
    """(ok, detail). Retries while the answer is not yet the expected build.
    `expect_env`: the deployment must call itself that (production: anything
    else means its scheduler and every send stand down — config.deploy_env)."""
    last = "no answer"
    for attempt in range(max(1, retries)):
        try:
            status, _h, body = fetch("/health", {})
        except Exception as e:
            status, body, last = None, b"", f"unreachable: {e}"
        data = _json(body) or {}
        build = str(data.get("build") or "")
        if status == 200:
            if not build or build == "unknown":
                last = "health names no build (a deploy that is not a commit — A05-13)"
            elif expect_sha and not (expect_sha.startswith(build) or build.startswith(expect_sha[:12])):
                last = f"health says build {build}, expected {expect_sha[:12]}"
            elif expect_env and str(data.get("env") or "") != expect_env:
                return False, (f"health says env {data.get('env')!r}, expected {expect_env!r} — the scheduler and "
                               "every send are off in a non-production environment (CAVNAR_ENV / Railway's name)")
            else:
                return True, f"health {data.get('status')} build {build}" + (
                    f" problems {data.get('problems')}" if data.get("problems") else "")
        elif status is not None:
            last = f"health answered {status} ({data.get('error') or ''})"
        if attempt < retries - 1:
            sleep(pause)
    return False, last


def check_login(fetch):
    """(ok, detail, the page's static paths)."""
    status, _h, body = fetch("/login", {})
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    if status != 200:
        return False, f"/login answered {status}", []
    if "password" not in text.lower():
        return False, "/login has no sign-in form", []
    return True, "/login 200", sorted(set(_ASSET.findall(text)))


def check_assets(fetch, paths, limit=4):
    if not paths:
        return False, "/login references no static file"
    bad = []
    for path in paths[:limit]:
        status, _h, _b = fetch(path, {})
        if status != 200:
            bad.append(f"{path} {status}")
    return (not bad), (f"{min(len(paths), limit)} static file(s) 200" if not bad else "static: " + ", ".join(bad))


def check_reads(fetch, token, mobile_token=None):
    """One web read and one mobile read as the smoke tenant."""
    out = []
    status, headers, body = fetch("/api/home/brief", {"Cookie": f"session_token={token}"})
    ctype = str({k.lower(): v for k, v in headers.items()}.get("content-type", ""))
    if status != 200 or "json" not in ctype:
        out.append(f"web read /api/home/brief {status}")
    status, headers, body = fetch("/mobile/api/me", {"Authorization": f"Bearer {mobile_token or token}"})
    data = _json(body) or {}
    if status != 200 or not data.get("ok"):
        out.append(f"mobile read /mobile/api/me {status}")
    return (not out), ("web and mobile reads 200" if not out else "; ".join(out))


def run(fetch, expect_sha=None, token=None, mobile_token=None, out=print, sleep=time.sleep,
        retries=HEALTH_RETRIES, expect_env=None):
    """Every check; returns the exit status."""
    results = []
    ok, detail = check_health(fetch, expect_sha, retries=retries, sleep=sleep, expect_env=expect_env)
    results.append(("health", ok, detail))
    ok, detail, assets = check_login(fetch)
    results.append(("login", ok, detail))
    if ok:
        results.append(("static",) + check_assets(fetch, assets))
    if token:
        results.append(("reads",) + check_reads(fetch, token, mobile_token))
    else:
        results.append(("reads", True, "skipped (no SMOKE_TOKEN)"))
    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        out(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    if failed:
        out(ROLLBACK_HINT)
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base_url")
    ap.add_argument("--expect-sha", default=os.getenv("EXPECTED_SHA") or None)
    ap.add_argument("--expect-env", default=None, help="the deploy_env /health must report (e.g. production)")
    args = ap.parse_args(argv)
    return run(http_fetch(args.base_url), expect_sha=args.expect_sha, expect_env=args.expect_env,
               token=(os.getenv("SMOKE_TOKEN") or "").strip() or None,
               mobile_token=(os.getenv("SMOKE_MOBILE_TOKEN") or "").strip() or None)


if __name__ == "__main__":
    sys.exit(main())
