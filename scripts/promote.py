"""
scripts/promote.py — move production to a main SHA that earned it (audit #25,
deployment strategy: "Promote once a day, 9am-3pm CT"; A05-01, R2, R4).

Railway's production service tracks the `production` branch, so pushing to
main no longer deploys to customers. Promotion fast-forwards `production` to
a SHA, and refuses unless:

  1. the SHA is on main (an ancestor of origin/main);
  2. the SHA has a successful `gate` check run (.github/workflows/ci.yml);
  and, unless --emergency:
  3. it is 9:00-15:00 America/Chicago, Monday to Friday (no deploys during
     dinner service — 58% of deploys landed 5pm-2am CT before this);
  4. the latest finished nightly run is green, or every test that failed in
     it is marked `quarantine` on main now (a flaky test is quarantined the
     day it flakes; TESTING.md).

--emergency skips the window and the nightly, never the gate or main.

    python scripts/promote.py --dry-run                 # main's head, decide only
    python scripts/promote.py --sha abc1234 --dry-run
    python scripts/promote.py --sha abc1234             # decide, then push
    python scripts/promote.py --emergency --sha abc1234

GitHub is read through the `gh` CLI (GH_TOKEN in Actions). The push is
`git push origin <sha>:refs/heads/production` — a fast-forward, or git
refuses. The nightly's own job uses --summarize-junit to write the summary
this script reads.
"""
import argparse
import ast
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHICAGO = ZoneInfo("America/Chicago")
WINDOW = (9, 15)                       # 9:00 <= local time < 15:00
NIGHTLY_WORKFLOW = "nightly.yml"
SUMMARY_ARTIFACT = "nightly-summary"


# ── the decision (pure; tests/test_promote.py) ───────────────────────────────

def in_window(now_local):
    """Monday-Friday, 9:00 to 15:00 in Chicago."""
    return now_local.weekday() < 5 and WINDOW[0] <= now_local.hour < WINDOW[1]


def gate_passed(check_runs):
    """A completed, successful check run named `gate`."""
    return any(r.get("name") == "gate" and r.get("status") == "completed" and r.get("conclusion") == "success"
               for r in check_runs or ())


def is_quarantined(nodeid, quarantined):
    """Whether a failed test id is covered by a quarantine mark: its whole
    file, or the test (any parametrisation of it)."""
    path, _, name = nodeid.partition("::")
    if path in quarantined:
        return True
    base = name.split("[", 1)[0]
    return f"{path}::{base}" in quarantined


def nightly_ok(nightly, quarantined):
    """(ok, why) for the latest finished nightly run."""
    if not nightly:
        return False, "no finished nightly run on main"
    if nightly.get("conclusion") == "success":
        return True, f"nightly {nightly.get('id')} is green"
    summary = nightly.get("summary")
    if summary is None:
        return False, f"nightly {nightly.get('id')} is {nightly.get('conclusion')} and has no summary to triage"
    failed = summary.get("failed") or []
    broken = [j for j, r in (summary.get("jobs") or {}).items()
              if r in ("failure", "cancelled") and j not in summary.get("jobs_with_test_failures", [])
              and j != "quarantine"]
    if broken:
        return False, f"nightly {nightly.get('id')}: jobs failed without a failing test (infrastructure?): {broken}"
    if not failed:
        return False, f"nightly {nightly.get('id')} is {nightly.get('conclusion')} with no failed test recorded"
    open_ = [t for t in failed if not is_quarantined(t, quarantined)]
    if open_:
        return False, f"nightly {nightly.get('id')}: {len(open_)} failure(s) not quarantined, e.g. {open_[:3]}"
    return True, f"nightly {nightly.get('id')}: every failure is quarantined ({len(failed)})"


def decide(*, on_main, check_runs, now_local, emergency, nightly, quarantined):
    """{"ok": bool, "reasons": [why not], "notes": [what held]}"""
    reasons, notes = [], []
    if not on_main:
        reasons.append("the SHA is not on main")
    if gate_passed(check_runs):
        notes.append("gate passed")
    else:
        reasons.append("the SHA has no successful `gate` check run")
    if emergency:
        notes.append("emergency: the window and the nightly are skipped (never the gate)")
    else:
        if in_window(now_local):
            notes.append(f"inside the window ({now_local:%a %H:%M} Chicago)")
        else:
            reasons.append(f"outside the promotion window: {now_local:%a %H:%M} in Chicago "
                           f"(Monday-Friday {WINDOW[0]}:00-{WINDOW[1]}:00); --emergency to override")
        ok, why = nightly_ok(nightly, quarantined)
        (notes if ok else reasons).append(why)
    return {"ok": not reasons, "reasons": reasons, "notes": notes}


# ── what is quarantined at a SHA ─────────────────────────────────────────────

def _is_quarantine_mark(node):
    if isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Attribute) and node.attr == "quarantine" and \
        isinstance(node.value, ast.Attribute) and node.value.attr == "mark"


def quarantined_tests(root=ROOT):
    """{"tests/test_x.py", "tests/test_y.py::test_z", ...} — files carrying
    a module-level quarantine mark, and tests (or classes) decorated with it."""
    out = set()
    tests = os.path.join(root, "tests")
    for dirpath, dirnames, filenames in os.walk(tests):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "fixtures")]
        for name in filenames:
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            if "quarantine" not in text:
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            tree = ast.parse(text)
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "pytestmark" for t in node.targets):
                    vals = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
                    if any(_is_quarantine_mark(v) for v in vals):
                        out.add(rel)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and \
                        any(_is_quarantine_mark(d) for d in node.decorator_list):
                    out.add(f"{rel}::{node.name}")
    return out


# ── the nightly's summary (written by the nightly's `summary` job) ───────────

def summarize_junit(directory, job_results=None):
    """{"failed": [nodeid], "jobs": {job: result}, "jobs_with_test_failures": [job]}
    from every junit XML under `directory` (one per nightly job)."""
    failed, with_failures = [], set()
    for dirpath, _dirs, files in os.walk(directory):
        for name in sorted(files):
            if not name.endswith(".xml"):
                continue
            job = name[:-4]
            job = "postgres" if job.startswith("postgres-") else job
            try:
                root = ET.parse(os.path.join(dirpath, name)).getroot()
            except ET.ParseError:
                continue
            for case in root.iter("testcase"):
                if case.find("failure") is not None or case.find("error") is not None:
                    failed.append(_nodeid(case))
                    with_failures.add(job)
    jobs = {k: (v or {}).get("result") for k, v in (job_results or {}).items()}
    return {"failed": sorted(set(failed)), "jobs": jobs, "jobs_with_test_failures": sorted(with_failures)}


def _nodeid(case):
    """pytest's junit classname is the dotted path ("tests.test_x" or
    "tests.test_x.TestClass"); back to "tests/test_x.py::name"."""
    cls = case.get("classname", "")
    name = case.get("name", "")
    parts = [p for p in cls.split(".") if p]
    for i in range(len(parts), 0, -1):
        candidate = "/".join(parts[:i]) + ".py"
        if os.path.exists(os.path.join(ROOT, candidate)):
            return "::".join([candidate] + parts[i:] + [name])
    # Not in this checkout: the module is the part that names a test file.
    for i, part in enumerate(parts):
        if part.startswith("test_"):
            return "::".join(["/".join(parts[:i + 1]) + ".py"] + parts[i + 1:] + [name])
    return f"{cls}::{name}"


# ── GitHub and git (behind one runner, so --dry-run and tests fake them) ─────

def _run(args, timeout=60):
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _gh_json(run, args):
    code, out, err = run(["gh", "api", *args])
    if code != 0:
        raise RuntimeError(f"gh api {' '.join(args)} failed: {err.strip()[:300]}")
    return json.loads(out or "null")


def gather(sha, repo, run=_run):
    """Everything decide() reads, from git and GitHub."""
    code, _o, _e = run(["git", "merge-base", "--is-ancestor", sha, "origin/main"])
    on_main = code == 0
    checks = _gh_json(run, [f"repos/{repo}/commits/{sha}/check-runs?check_name=gate&per_page=100"])
    runs = _gh_json(run, [f"repos/{repo}/actions/workflows/{NIGHTLY_WORKFLOW}/runs?branch=main&status=completed"
                          "&per_page=1"])
    latest = (runs or {}).get("workflow_runs") or []
    nightly = None
    if latest:
        w = latest[0]
        nightly = {"id": w.get("id"), "conclusion": w.get("conclusion"), "head_sha": w.get("head_sha"),
                   "summary": None}
        if w.get("conclusion") != "success":
            nightly["summary"] = _download_summary(run, repo, w.get("id"))
    return {"on_main": on_main, "check_runs": (checks or {}).get("check_runs") or [], "nightly": nightly}


def _download_summary(run, repo, run_id):
    import tempfile
    d = tempfile.mkdtemp(prefix="nightly-summary-")
    code, _o, _e = run(["gh", "run", "download", str(run_id), "-R", repo, "-n", SUMMARY_ARTIFACT, "-D", d])
    path = os.path.join(d, "nightly-summary.json")
    if code != 0 or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def resolve_sha(sha, run=_run):
    code, out, err = run(["git", "rev-parse", "--verify", (sha or "origin/main") + "^{commit}"])
    if code != 0:
        raise RuntimeError(f"unknown SHA {sha!r}: {err.strip()[:200]}")
    return out.strip()


def push_command(sha):
    return ["git", "push", "origin", f"{sha}:refs/heads/production"]


def main(argv=None, run=_run, now=None):
    ap = argparse.ArgumentParser(description="Promote a main SHA to production.")
    ap.add_argument("--sha", default="")
    ap.add_argument("--emergency", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    ap.add_argument("--summarize-junit", default=None, help="(the nightly's summary job) a junit directory")
    ap.add_argument("--out", default="nightly-summary.json")
    args = ap.parse_args(argv)

    if args.summarize_junit is not None:
        results = json.loads(os.getenv("JOB_RESULTS") or "{}")
        summary = summarize_junit(args.summarize_junit, results)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)
        print(f"{len(summary['failed'])} failed test(s); jobs {summary['jobs']}")
        return 0

    if not args.repo:
        print("--repo owner/name (or GITHUB_REPOSITORY) is required", file=sys.stderr)
        return 2
    sha = resolve_sha(args.sha, run)
    facts = gather(sha, args.repo, run)
    now_local = (now or datetime.now(CHICAGO)).astimezone(CHICAGO)
    verdict = decide(on_main=facts["on_main"], check_runs=facts["check_runs"], now_local=now_local,
                     emergency=args.emergency, nightly=facts["nightly"], quarantined=quarantined_tests())
    print(f"Promote {sha[:12]} to production: {'YES' if verdict['ok'] else 'NO'}")
    for n in verdict["notes"]:
        print(f"  ok   {n}")
    for r in verdict["reasons"]:
        print(f"  no   {r}")
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"### Promote {sha[:12]}: {'yes' if verdict['ok'] else 'refused'}\n")
            fh.writelines(f"- {line}\n" for line in verdict["notes"] + verdict["reasons"])
    if not verdict["ok"]:
        return 1
    cmd = push_command(sha)
    if args.dry_run:
        print("  dry run — would run: " + " ".join(cmd))
        return 0
    code, out, err = run(cmd, timeout=120)
    print(out.strip() or err.strip())
    return 0 if code == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
