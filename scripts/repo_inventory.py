#!/usr/bin/env python3
"""
repo_inventory.py — the numbers the reference docs used to hand-type.

Prints tables, routes by blueprint, scheduled jobs, model call sites and
the test count from the code itself, so a doc can quote the command
instead of a figure that is stale within days.

    python3 scripts/repo_inventory.py            # everything but the test count
    python3 scripts/repo_inventory.py --tests    # also collect the test count (~2s)

Read-only. Importing the app boots it against a scratch volume; the real
reviews.db is never opened.
"""
import os, re, subprocess, sys, tempfile
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)


def _py_files():
    out = subprocess.check_output(["git", "ls-files", "*.py"], text=True).split()
    return [f for f in out if not f.startswith("tests/")]


def tables():
    names = set()
    for f in _py_files():
        src = open(f, encoding="utf-8").read()
        names.update(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", src))
    names -= {"then", "silently", "ensure", "on"}   # words inside prose that mention the phrase
    return sorted(names)


def routes():
    tmp = tempfile.mkdtemp(prefix="cavnar-inventory-")
    env = dict(os.environ, RAILWAY_VOLUME_MOUNT_PATH=tmp, RUN_SCHEDULER_IN_WEB="0",
               ANTHROPIC_API_KEY="", RESEND_API_KEY="", ALLOW_LOCAL_SCHEDULER="0")
    code = (
        "import hosted_dashboard as h, json\n"
        "rows=[(r.endpoint.split('.')[0] if '.' in r.endpoint else 'app', r.rule, sorted(m for m in r.methods if m not in ('HEAD','OPTIONS'))) for r in h.app.url_map.iter_rules()]\n"
        "print(json.dumps(rows))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    if out.returncode != 0:
        return None, out.stderr[-600:]
    import json
    return json.loads(out.stdout.strip().splitlines()[-1]), None


def jobs():
    src = open("scheduler.py", encoding="utf-8").read()
    return sorted(set(re.findall(r'claim_period\("([a-z_]+)"', src)))


def model_calls():
    hits = []
    for f in _py_files():
        if f == "ai_utils.py":
            continue
        for i, line in enumerate(open(f, encoding="utf-8"), 1):
            m = re.search(r'getenv\("([A-Z_]*MODEL)"\s*,\s*"([^"]+)"\)', line) or re.search(r'model\s*=\s*"(claude-[^"]+)"', line)
            if m:
                hits.append((f, i, line.strip()[:110]))
    return hits


def main():
    t = tables()
    print(f"tables: {len(t)}")
    print("  " + " ".join(t))
    rows, err = routes()
    if rows is None:
        print(f"routes: could not boot the app: {err}")
    else:
        by = Counter(bp for bp, _r, _m in rows)
        print(f"\nroutes: {len(rows)} rules, {len({r for _b, r, _m in rows})} unique paths")
        for bp, n in sorted(by.items(), key=lambda x: -x[1]):
            print(f"  {bp:18} {n}")
    j = jobs()
    print(f"\nscheduled job claim keys: {len(j)}")
    print("  " + " ".join(j))
    mc = model_calls()
    print(f"\nmodel call sites: {len(mc)}")
    for f, i, line in mc:
        print(f"  {f}:{i}  {line}")
    if "--tests" in sys.argv:
        out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                             capture_output=True, text=True)
        print("\n" + out.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    main()
