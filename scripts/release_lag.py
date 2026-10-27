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

    python scripts/release_lag.py https://dashboard.cavnar.ai --head-sha SHA --head-time EPOCH \
        [--repo OWNER/NAME --branch main]

The lag runs from when the head was PUSHED, not from its commit time (blind
re-audit 10/27/26 B5 P3: a commit made an hour before its push alarmed on
the first hourly run, deploy healthy or not). With --repo (and GITHUB_TOKEN)
the push time is GitHub's own record of the push that moved the branch to
the head (the repository activity API), else the moment Railway created the
head's deployment (its `created_at`, seconds after the push), and only when
neither answers the commit time given as --head-time, which the output says.

Reads only; writes and sends nothing.
"""
import argparse
import json
import sys
import time

TIMEOUT = 15
MAX_LAG_MINUTES = 60


def verdict(build, head_sha, head_time, now, max_lag_minutes=MAX_LAG_MINUTES):
    """(ok, message). `build` is /health's (12 characters or "unknown");
    `head_time` is when the head was pushed (pushed_at), else committed."""
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


def _epoch(stamp):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def push_time_from_activities(activities, sha):
    """The push that moved the branch to `sha` (GitHub's "list repository
    activities", newest first): its time, Unix seconds, or None."""
    for item in activities or ():
        if isinstance(item, dict) and str(item.get("after") or "") == sha and \
                item.get("activity_type", "push") in ("push", "force_push", "merge_queue_merge"):
            return _epoch(item.get("timestamp"))
    return None


def push_time_from_deployments(deployments, sha):
    """The earliest deployment GitHub records for `sha` (Railway creates one
    seconds after the push): its created_at, Unix seconds, or None."""
    times = [_epoch(d.get("created_at")) for d in deployments or () if isinstance(d, dict) and d.get("sha") == sha]
    times = [t for t in times if t is not None]
    return min(times) if times else None


def _github(path, token):
    import requests
    r = requests.get("https://api.github.com" + path, timeout=TIMEOUT,
                     headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                              "X-GitHub-Api-Version": "2022-11-28"})
    r.raise_for_status()
    return r.json()


def pushed_at(repo, branch, sha, token):
    """(push time, how it was found) for `sha` on `branch`, or (None, why
    not). Never raises."""
    if not (repo and token):
        return None, "no --repo or GITHUB_TOKEN"
    try:
        t = push_time_from_activities(_github(f"/repos/{repo}/activity?ref=refs/heads/{branch}"
                                              f"&activity_type=push&per_page=30", token), sha)
        if t is not None:
            return t, "the push"
    except Exception as e:
        print(f"note: the activity API did not answer ({type(e).__name__}: {e})")
    try:
        t = push_time_from_deployments(_github(f"/repos/{repo}/deployments?sha={sha}&per_page=30", token), sha)
        if t is not None:
            return t, "its deployment's creation"
    except Exception as e:
        print(f"note: the deployments API did not answer ({type(e).__name__}: {e})")
    return None, "neither the push nor a deployment found"


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
    ap.add_argument("--head-time", type=float, required=True,
                    help="the head commit's time, Unix seconds — the fallback when the push time is not found")
    ap.add_argument("--repo", default=None, help="OWNER/NAME: read the push time from GitHub (GITHUB_TOKEN)")
    ap.add_argument("--branch", default="main", help="the branch Railway deploys")
    ap.add_argument("--max-lag-minutes", type=float, default=MAX_LAG_MINUTES)
    args = ap.parse_args(argv)
    import os
    since, how = pushed_at(args.repo, args.branch, args.head_sha, os.getenv("GITHUB_TOKEN") or "")
    if since is None:
        print(f"note: measuring from the commit time ({how})")
        since = args.head_time
    else:
        print(f"note: measuring from {how}")
    ok, message = verdict(deployed_build(args.base_url), args.head_sha, since, time.time(), args.max_lag_minutes)
    print(("OK " if ok else "LAG ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
