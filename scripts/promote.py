"""
scripts/promote.py — move production to a main SHA that earned it (audit #25,
deployment strategy: "Promote once a day, 9am-3pm CT"; A05-01, R2, R4; blind
re-audit 10/27/26 B1 F3/F16, B2, B4, B5, B6).

Once Railway's production service deploys the `production` branch
(docs/ops/RECOVERY.md, "Deploys"), a push to main deploys nothing and this is
the only way a change reaches customers. Until that switch is made, a push to
main still deploys at once and this script only moves the branch. Promotion
fast-forwards `production` to a SHA, and refuses unless:

  1. the SHA is on main (an ancestor of origin/main);
  2. the SHA has successful `gate`, `gate-postgres` and `gate-boot` check
     runs (.github/workflows/ci.yml);
  and, unless --emergency:
  3. it is 9:00-15:00 America/Chicago, Monday to Friday (no deploys during
     dinner service — 58% of deploys landed 5pm-2am CT before this);
  4. a nightly ran ON THIS SHA (the run tested this very commit, not the
     latest nightly on main, which tested an older one) and is green, or
     every test that failed in it is marked `quarantine` AT THIS SHA with a
     reason and a date younger than QUARANTINE_MAX_DAYS. No nightly on the
     SHA yet: the script dispatches one (nightly.yml, input `sha`) and
     refuses until it is green — run it again then;
  5. when the promotion carries a change under ios/ (production..SHA, or
     always while `production` does not exist), the iOS workflow ran on this
     SHA and is green — dispatched the same way when it has not.

--emergency skips the window, the nightly and the iOS run, never the gate or
main. The verdict also warns, without refusing, while `production` does not
exist or is not a protected branch.

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
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHICAGO = ZoneInfo("America/Chicago")
WINDOW = (9, 15)                       # 9:00 <= local time < 15:00
NIGHTLY_WORKFLOW = "nightly.yml"
IOS_WORKFLOW = "ios.yml"
SUMMARY_ARTIFACT = "nightly-summary"
REQUIRED_CHECKS = ("gate", "gate-postgres", "gate-boot")
# A quarantine is a deadline: a failure whose quarantine mark is older than
# this is not excused (TESTING.md, "Markers").
QUARANTINE_MAX_DAYS = 14
_RUNNING = ("queued", "in_progress", "waiting", "requested", "pending")
_SHA_IN_TITLE = re.compile(r"\b([0-9a-f]{40})\b")


# ── the decision (pure; tests/test_promote.py) ───────────────────────────────

def in_window(now_local):
    """Monday-Friday, 9:00 to 15:00 in Chicago."""
    return now_local.weekday() < 5 and WINDOW[0] <= now_local.hour < WINDOW[1]


def missing_checks(check_runs, required=REQUIRED_CHECKS):
    """The required check runs without a completed, successful run."""
    green = {r.get("name") for r in check_runs or ()
             if r.get("status") == "completed" and r.get("conclusion") == "success"}
    return [name for name in required if name not in green]


def gate_passed(check_runs):
    """Every required gate check run completed successfully."""
    return not missing_checks(check_runs)


def is_quarantined(nodeid, quarantined):
    """Whether a failed test id is covered by a quarantine mark: its whole
    file, or the test (any parametrisation of it)."""
    path, _, name = nodeid.partition("::")
    if path in quarantined:
        return True
    base = name.split("[", 1)[0]
    return f"{path}::{base}" in quarantined


def nightly_ok(nightly, quarantined, sha=None):
    """(ok, why) for the nightly that ran on `sha`."""
    short = (sha or "")[:12]
    if not nightly:
        return False, f"no finished nightly has run on {short or 'this SHA'}"
    if sha and nightly.get("tested_sha") != sha:
        return False, (f"nightly {nightly.get('id')} tested {str(nightly.get('tested_sha'))[:12]}, not {short}: "
                       "only a nightly on this very SHA counts")
    if nightly.get("conclusion") == "success":
        return True, f"nightly {nightly.get('id')} on {short} is green"
    summary = nightly.get("summary")
    if summary is None:
        return False, f"nightly {nightly.get('id')} is {nightly.get('conclusion')} and has no summary to triage"
    if sha and summary.get("sha") not in (None, sha):
        return False, f"nightly {nightly.get('id')}'s summary is about {str(summary.get('sha'))[:12]}, not {short}"
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


def decide(*, on_main, check_runs, now_local, emergency, nightly, quarantined, sha=None, nightly_running=False,
           ios=None):
    """{"ok": bool, "reasons": [why not], "notes": [what held]}.

    `ios` is None when the promotion carries nothing under ios/, else
    {"run": the iOS run on this SHA or None, "running": bool}."""
    reasons, notes = [], []
    if not on_main:
        reasons.append("the SHA is not on main")
    missing = missing_checks(check_runs)
    if missing:
        reasons.append(f"the SHA has no successful {' / '.join('`%s`' % m for m in missing)} check run")
    else:
        notes.append("gate, gate-postgres and gate-boot passed")
    if emergency:
        notes.append("emergency: the window, the nightly and the iOS run are skipped (never the gate)")
        return {"ok": not reasons, "reasons": reasons, "notes": notes}
    if in_window(now_local):
        notes.append(f"inside the window ({now_local:%a %H:%M} Chicago)")
    else:
        reasons.append(f"outside the promotion window: {now_local:%a %H:%M} in Chicago "
                       f"(Monday-Friday {WINDOW[0]}:00-{WINDOW[1]}:00); --emergency to override")
    if nightly is None and nightly_running:
        reasons.append(f"a nightly on {(sha or '')[:12]} is running; promote again when it is green")
    else:
        ok, why = nightly_ok(nightly, quarantined, sha)
        (notes if ok else reasons).append(why)
    if ios is not None:
        run = ios.get("run")
        if run and run.get("conclusion") == "success":
            notes.append(f"the iOS tests passed on this SHA (run {run.get('id')})")
        elif run:
            reasons.append(f"the iOS tests on this SHA are {run.get('conclusion')} (run {run.get('id')}); this "
                           "promotion carries a change under ios/")
        elif ios.get("running"):
            reasons.append("the iOS tests on this SHA are running; promote again when they are green")
        else:
            reasons.append("this promotion carries a change under ios/ and no iOS run on this SHA has finished")
    return {"ok": not reasons, "reasons": reasons, "notes": notes}


# ── what is quarantined at a SHA ─────────────────────────────────────────────

def _quarantine_kwargs(node):
    """(reason, since) of a quarantine mark node, or None when it is not one."""
    call = node if isinstance(node, ast.Call) else None
    func = node.func if call else node
    if not (isinstance(func, ast.Attribute) and func.attr == "quarantine" and
            isinstance(func.value, ast.Attribute) and func.value.attr == "mark"):
        return None
    kw = {k.arg: k.value.value for k in (call.keywords if call else [])
          if isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)}
    return kw.get("reason") or "", kw.get("since") or ""


def quarantine_marks(files):
    """{"tests/test_x.py" or "tests/test_y.py::test_z": (reason, since)} from
    {relative path: source text}."""
    out = {}
    for rel, text in files.items():
        if "quarantine" not in text:
            continue
        tree = ast.parse(text)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "pytestmark" for t in node.targets):
                vals = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
                for v in vals:
                    got = _quarantine_kwargs(v)
                    if got is not None:
                        out[rel] = got
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for d in node.decorator_list:
                    got = _quarantine_kwargs(d)
                    if got is not None:
                        out[f"{rel}::{node.name}"] = got
    return out


def _test_files_on_disk(root):
    files = {}
    tests = os.path.join(root, "tests")
    for dirpath, dirnames, filenames in os.walk(tests):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "fixtures")]
        for name in filenames:
            if name.startswith("test_") and name.endswith(".py"):
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as fh:
                    files[os.path.relpath(path, root).replace(os.sep, "/")] = fh.read()
    return files


def quarantined_tests(root=ROOT):
    """{"tests/test_x.py", "tests/test_y.py::test_z", ...} — files carrying
    a module-level quarantine mark, and tests (or classes) decorated with it,
    in the tree at `root`."""
    return set(quarantine_marks(_test_files_on_disk(root)))


def quarantine_files_at(sha, run):
    """{relative path: text} of the test files that mention quarantine AT
    `sha` (what the nightly ran), not in this checkout."""
    code, out, _err = run(["git", "grep", "-l", "-e", "quarantine", sha, "--", "tests"])
    files = {}
    if code not in (0, 1):
        raise RuntimeError(f"git grep at {sha[:12]} failed")
    for line in out.splitlines():
        path = line.split(":", 1)[1] if line.startswith(sha + ":") else line
        name = os.path.basename(path)
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        c, text, _e = run(["git", "show", f"{sha}:{path}"])
        if c == 0:
            files[path] = text
    return files


def live_quarantine(marks, today):
    """(ids a failure may be excused by, [why each other mark is not]): a
    mark excuses only with a reason and a `since` date (M/D/YY) at most
    QUARANTINE_MAX_DAYS old on `today`."""
    live, stale = set(), []
    for nodeid, (reason, since) in sorted(marks.items()):
        try:
            age = (today - datetime.strptime(since, "%m/%d/%y").date()).days
        except ValueError:
            stale.append(f"{nodeid}: quarantined with no `since` date")
            continue
        if not reason.strip():
            stale.append(f"{nodeid}: quarantined with no reason")
        elif age > QUARANTINE_MAX_DAYS:
            stale.append(f"{nodeid}: quarantined {age} days ago (since {since}; at most {QUARANTINE_MAX_DAYS})")
        else:
            live.add(nodeid)
    return live, stale


# ── the nightly's summary (written by the nightly's `summary` job) ───────────

def summarize_junit(directory, job_results=None, sha=None):
    """{"failed": [nodeid], "jobs": {job: result}, "jobs_with_test_failures": [job],
    "sha": the commit the night tested} from every junit XML under
    `directory` (one per nightly job)."""
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
    return {"failed": sorted(set(failed)), "jobs": jobs, "jobs_with_test_failures": sorted(with_failures),
            "sha": sha or None}


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


def tested_sha(workflow_run):
    """The commit a workflow run tested: the SHA its run-name carries (a run
    dispatched with an input `sha` checks that commit out while its head_sha
    is main's head), else its head_sha."""
    m = _SHA_IN_TITLE.search(workflow_run.get("display_title") or workflow_run.get("name") or "")
    return m.group(1) if m else workflow_run.get("head_sha")


def runs_on(run, repo, workflow, sha):
    """(the newest finished run of `workflow` that tested `sha` or None,
    whether one is still running)."""
    data = _gh_json(run, [f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=60"])
    mine = [w for w in (data or {}).get("workflow_runs") or [] if tested_sha(w) == sha]
    running = any(w.get("status") in _RUNNING for w in mine)
    done = [w for w in mine if w.get("status") == "completed"]
    done.sort(key=lambda w: (w.get("run_started_at") or w.get("created_at") or "", w.get("id") or 0), reverse=True)
    return (done[0] if done else None), running


def ios_touched(sha, run):
    """Whether promoting `sha` carries a change under ios/: production..sha.
    None while `production` does not exist (taken as yes)."""
    code, _o, _e = run(["git", "rev-parse", "--verify", "--quiet", "origin/production^{commit}"])
    if code != 0:
        return None
    code, out, _e = run(["git", "diff", "--name-only", "origin/production", sha])
    if code != 0:
        return None
    return any(line.startswith("ios/") for line in out.splitlines())


def production_branch(run, repo):
    """{"exists": bool, "protected": bool} for the `production` branch."""
    code, out, err = run(["gh", "api", f"repos/{repo}/branches/production"])
    if code != 0:
        return {"exists": False, "protected": False}
    data = json.loads(out or "{}")
    return {"exists": True, "protected": bool(data.get("protected"))}


def gather(sha, repo, run=_run):
    """Everything decide() reads, from git and GitHub."""
    code, _o, _e = run(["git", "merge-base", "--is-ancestor", sha, "origin/main"])
    on_main = code == 0
    checks = _gh_json(run, [f"repos/{repo}/commits/{sha}/check-runs?per_page=100"])
    w, nightly_running = runs_on(run, repo, NIGHTLY_WORKFLOW, sha)
    nightly = None
    if w:
        nightly = {"id": w.get("id"), "conclusion": w.get("conclusion"), "head_sha": w.get("head_sha"),
                   "tested_sha": tested_sha(w), "summary": None}
        if w.get("conclusion") != "success":
            nightly["summary"] = _download_summary(run, repo, w.get("id"))
    touched = ios_touched(sha, run)
    ios = None
    if touched is not False:
        ios_run, ios_running = runs_on(run, repo, IOS_WORKFLOW, sha)
        ios = {"run": ios_run, "running": ios_running, "unknown_base": touched is None}
    return {"on_main": on_main, "check_runs": (checks or {}).get("check_runs") or [], "nightly": nightly,
            "nightly_running": nightly_running, "ios": ios, "production": production_branch(run, repo)}


def _download_summary(run, repo, run_id):
    import tempfile
    d = tempfile.mkdtemp(prefix="nightly-summary-")
    code, _o, _e = run(["gh", "run", "download", str(run_id), "-R", repo, "-n", SUMMARY_ARTIFACT, "-D", d])
    path = os.path.join(d, "nightly-summary.json")
    if code != 0 or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dispatch_command(workflow, sha, repo):
    """The workflow, on main, testing `sha` (its `sha` input)."""
    return ["gh", "workflow", "run", workflow, "-R", repo, "--ref", "main", "-f", f"sha={sha}"]


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
        summary = summarize_junit(args.summarize_junit, results, sha=(os.getenv("TESTED_SHA") or "").strip())
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)
        print(f"{len(summary['failed'])} failed test(s) on {summary['sha']}; jobs {summary['jobs']}")
        return 0

    if not args.repo:
        print("--repo owner/name (or GITHUB_REPOSITORY) is required", file=sys.stderr)
        return 2
    sha = resolve_sha(args.sha, run)
    facts = gather(sha, args.repo, run)
    now_local = (now or datetime.now(CHICAGO)).astimezone(CHICAGO)
    marks = quarantine_marks(quarantine_files_at(sha, run)) if not args.emergency else {}
    quarantined, stale = live_quarantine(marks, now_local.date())
    verdict = decide(on_main=facts["on_main"], check_runs=facts["check_runs"], now_local=now_local,
                     emergency=args.emergency, nightly=facts["nightly"], quarantined=quarantined, sha=sha,
                     nightly_running=facts["nightly_running"], ios=facts["ios"])
    warnings = [f"quarantine not honoured: {s}" for s in stale]
    prod = facts["production"]
    if not prod["exists"]:
        warnings.append("the `production` branch does not exist yet: this promotion creates it. Until Railway's "
                        "production service deploys it, a push to main still deploys (docs/ops/RECOVERY.md)")
    elif not prod["protected"]:
        warnings.append("the `production` branch is not protected: require `gate`, `gate-postgres` and "
                        "`gate-boot` and let only GitHub Actions push it (docs/ops/RECOVERY.md)")
    if facts["ios"] and facts["ios"].get("unknown_base"):
        warnings.append("no `production` to compare with: the iOS run is required for this promotion")

    # A nightly (or iOS run) this SHA has not had is started now, so the next
    # try can pass; never while one is running, never on a dry run.
    dispatched = []
    if facts["on_main"] and not args.emergency:
        if facts["nightly"] is None and not facts["nightly_running"]:
            dispatched.append(dispatch_command(NIGHTLY_WORKFLOW, sha, args.repo))
        ios = facts["ios"]
        if ios is not None and ios.get("run") is None and not ios.get("running"):
            dispatched.append(dispatch_command(IOS_WORKFLOW, sha, args.repo))

    print(f"Promote {sha[:12]} to production: {'YES' if verdict['ok'] else 'NO'}")
    for n in verdict["notes"]:
        print(f"  ok   {n}")
    for r in verdict["reasons"]:
        print(f"  no   {r}")
    for w in warnings:
        print(f"  warn {w}")
    for cmd in dispatched:
        if args.dry_run:
            print("  dry run — would start: " + " ".join(cmd))
        else:
            code, out, err = run(cmd)
            print(f"  started {cmd[3]} on {sha[:12]}" if code == 0 else
                  f"  could not start {cmd[3]}: {(err or out).strip()[:200]}")
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"### Promote {sha[:12]}: {'yes' if verdict['ok'] else 'refused'}\n")
            fh.writelines(f"- {line}\n" for line in verdict["notes"] + verdict["reasons"] + warnings)
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
