#!/usr/bin/env python3
"""
scripts/run_schedule_benchmark.py — every archetype restaurant through the real
generation job on both code-only draft sources, and the comparison table
(schedule_benchmark; blind re-audit 10/11/26 ARCH-22, AI-28; contract #7).

    python3 scripts/run_schedule_benchmark.py                      # every archetype but mega_500, both arms, 3 runs
    python3 scripts/run_schedule_benchmark.py --only cafe_6 bistro_22 --runs 1
    python3 scripts/run_schedule_benchmark.py --heavy              # mega_500 too (needs ~8 GB)
    python3 scripts/run_schedule_benchmark.py --json out.json      # the raw results as well

Each (archetype, arm) runs in its own child process with its own throwaway
database (a fresh RAILWAY_VOLUME_MOUNT_PATH), no provider key in its
environment and outbound sockets refused — so its peak memory is that
generation's alone and nothing can reach a provider. Never reads or writes
anybody's reviews.db. The gate (schedule_benchmark.INVARIANTS) is printed
per row; the exit status is 0 whatever it says (a measurement, not a test —
tests/test_schedule_benchmark.py is the test).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STRIP = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PERPLEXITY_API_KEY", "RESEND_API_KEY", "TWILIO_AUTH_TOKEN",
          "TWILIO_ACCOUNT_SID", "STRIPE_SECRET_KEY", "DATABASE_URL", "GOOGLE_PLACES_API_KEY")


def _child_env(vol):
    env = {k: v for k, v in os.environ.items() if k not in _STRIP and not k.endswith("_API_KEY")}
    env["RAILWAY_VOLUME_MOUNT_PATH"] = vol
    env["PYTHONPATH"] = ROOT
    env.setdefault("TZ", "America/Chicago")
    return env


def _no_network():
    import socket

    def refuse(*_a, **_k):
        raise OSError("network disabled in the schedule benchmark")
    socket.socket.connect = refuse
    socket.create_connection = refuse


def _boot():
    _no_network()
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False       # never the checkout's .env
    sys.path.insert(0, ROOT)
    import models
    models.init_db()
    models.ensure_columns()
    for mod, fn in (("auth", "init_auth"), ("push", "init_push"), ("webhooks", "init_webhooks"),
                    ("guest_marketing", "init_guest_marketing")):
        getattr(__import__(mod), fn)()


def child(key, arm, runs, scale):
    """One (archetype, arm) in this process: prints one JSON line."""
    _boot()
    import schedule_benchmark as sb
    res = sb.run_archetype(key, arms=(arm,), runs=runs, scale=scale)
    res["arms"][arm]["peak_rss_mb"] = sb.peak_rss_mb()
    res["arms"][arm]["child_peak_rss_mb"] = sb.peak_rss_mb(children=True) or None
    print("BENCHMARK-RESULT " + json.dumps(res, default=str))


# What a plan entry's fifth element may set (schedule_benchmark.run_archetype).
_PLAN_OPTIONS = ("timezone", "week")


def child_batch(plan):
    """Several archetypes in this one process and database (the test's fast
    subset — one boot instead of one per arm): `plan` is [[key, [arms],
    runs, scale(, {"timezone", "week"})]]; prints one JSON line per
    archetype. Peak memory is the whole batch's, so it is not reported."""
    _boot()
    import schedule_benchmark as sb
    for item in plan:
        key, arms, runs, scale = item[:4]
        opts = {k: v for k, v in (item[4] if len(item) > 4 else {}).items() if k in _PLAN_OPTIONS}
        res = sb.run_archetype(key, arms=tuple(arms), runs=int(runs), scale=float(scale), **opts)
        print("BENCHMARK-RESULT " + json.dumps(res, default=str), flush=True)


def run_batch(plan, timeout=None) -> list:
    """child_batch in a fresh process with its own throwaway database, no
    provider key and no network: the results, one per archetype."""
    import shutil
    vol = tempfile.mkdtemp(prefix="cavnar-bench-")
    open(os.path.join(vol, "reviews.db"), "a").close()
    try:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child-batch", json.dumps(plan)], cwd=ROOT,
                           env=_child_env(vol), capture_output=True, text=True, timeout=timeout)
    finally:
        shutil.rmtree(vol, ignore_errors=True)
    out = [json.loads(ln[len("BENCHMARK-RESULT "):]) for ln in p.stdout.splitlines()
           if ln.startswith("BENCHMARK-RESULT ")]
    if len(out) != len(plan):
        raise RuntimeError(f"benchmark batch ended early (exit {p.returncode}): {(p.stderr or '')[-1500:]}")
    return out


def main(argv=None):
    sys.path.insert(0, ROOT)
    import schedule_benchmark as sb
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", nargs="*", help="archetype keys (default: all but the heavy ones)")
    ap.add_argument("--arms", nargs="*", default=list(sb.ARMS))
    ap.add_argument("--runs", type=int, default=sb.RUNS)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--heavy", action="store_true", help="include " + ", ".join(sb.HEAVY))
    ap.add_argument("--json", help="also write the raw results here")
    ap.add_argument("--child", nargs=2, metavar=("ARCHETYPE", "ARM"), help=argparse.SUPPRESS)
    ap.add_argument("--child-batch", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.child_batch:
        child_batch(json.loads(a.child_batch))
        return None
    if a.child:
        child(a.child[0], a.child[1], a.runs, a.scale)
        return None
    keys = a.only or [k for k in sb.ARCHETYPES if a.heavy or k not in sb.HEAVY]
    merged = {}
    for key in keys:
        for arm in a.arms:
            vol = tempfile.mkdtemp(prefix="cavnar-bench-")
            open(os.path.join(vol, "reviews.db"), "a").close()
            p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child", key, arm, "--runs",
                                str(a.runs), "--scale", str(a.scale)], cwd=ROOT, env=_child_env(vol),
                               capture_output=True, text=True)
            line = next((ln for ln in p.stdout.splitlines() if ln.startswith("BENCHMARK-RESULT ")), None)
            if line is None:
                res = {"key": key, "label": key, "people": 0, "arms": {arm: {"status": "crashed",
                                                                            "error": (p.stderr or "")[-300:]}},
                       "failed": {arm: ["saved"]}}
            else:
                res = json.loads(line[len("BENCHMARK-RESULT "):])
            m = merged.setdefault(key, dict(res, arms={}, failed={}))
            m["arms"][arm] = res["arms"][arm]
            m["failed"][arm] = res["failed"].get(arm, [])
            mm = res["arms"][arm]
            print(f"{key:20s} {arm:13s} {mm.get('status')}  {mm.get('seconds')}s  {mm.get('cpu_seconds')} CPU-s  "
                  f"{mm.get('peak_rss_mb')} MB (solve {mm.get('child_peak_rss_mb')} MB)  fails: {', '.join(res['failed'].get(arm) or []) or 'none'}",
                  flush=True)
            import shutil
            shutil.rmtree(vol, ignore_errors=True)
    results = list(merged.values())
    print()
    print(sb.table(results))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=1, default=str)
    return results


if __name__ == "__main__":
    main()
