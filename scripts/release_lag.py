"""
release_lag.py — is production running the commit it should be? (release-
safety audit 10/26/26, A07-01 / R15).

Run hourly by .github/workflows/release.yml: reads /health's `build` and
compares it with the head of the branch Railway deploys (`production` once
the promote flow is in place; `main` until then). A head that is newer than
MAX_LAG_MINUTES and still not live means a deploy stalled or failed (55
commits went undeployed 10/21–10/23 and were noticed by hand); a build of
"unknown" is a deploy that is not a commit. Either fails the run, and a
failed scheduled run emails the repository's owner.

    python scripts/release_lag.py https://dashboard.cavnar.ai --head-sha SHA --head-time EPOCH

Reads only; writes and sends nothing.
"""
import argparse
import json
import sys
import time

TIMEOUT = 15
MAX_LAG_MINUTES = 60


def verdict(build, head_sha, head_time, now, max_lag_minutes=MAX_LAG_MINUTES):
    """(ok, message). `build` is /health's (12 characters or "unknown")."""
    build = (build or "").strip()
    if not build or build == "unknown":
        return False, "production names no build: a deploy that is not a commit (docs/ops/RECOVERY.md)"
    if head_sha.startswith(build):
        return True, f"production runs the {head_sha[:12]} head"
    age_min = (now - head_time) / 60.0
    if age_min <= max_lag_minutes:
        return True, (f"production runs {build}; the head {head_sha[:12]} is {age_min:.0f} min old "
                      f"(within {max_lag_minutes})")
    return False, (f"production runs {build} but the head {head_sha[:12]} has waited {age_min:.0f} min "
                   f"(over {max_lag_minutes}): a deploy stalled or failed — check Railway's deployments")


def deployed_build(base_url):
    import requests
    r = requests.get(base_url.rstrip("/") + "/health", timeout=TIMEOUT)
    try:
        return (r.json() or {}).get("build") or ""
    except (ValueError, json.JSONDecodeError):
        return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base_url")
    ap.add_argument("--head-sha", required=True)
    ap.add_argument("--head-time", type=float, required=True, help="the head commit's time, Unix seconds")
    ap.add_argument("--max-lag-minutes", type=float, default=MAX_LAG_MINUTES)
    args = ap.parse_args(argv)
    ok, message = verdict(deployed_build(args.base_url), args.head_sha, args.head_time, time.time(),
                          args.max_lag_minutes)
    print(("OK " if ok else "LAG ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
