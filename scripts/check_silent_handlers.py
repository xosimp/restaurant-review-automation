#!/usr/bin/env python3
"""
check_silent_handlers.py — stop swallowed failures from creeping back in.

The pre-launch audit counted 179 `except Exception: pass` handlers; Audit #25
(10/26/26, A07-08) counted 6,315 broad handlers and about 1,900 that leave
no trace, in 357 files — and found this lint read only the top-level
modules and only the exact `except Exception: pass`. It now reads every
app module, the packages included (dsr/, intelligence/, bar/, demo_store/,
event_intel/, brand/, ...), and three rules hold:

1. A truly bare `except:` — it catches KeyboardInterrupt and SystemExit too,
   so it can swallow a deploy's shutdown signal or a Ctrl-C. There is never a
   reason for one; name the exceptions, or catch Exception. (An error.)

2. A broad handler whose body is only `pass`, wrapped around a database
   WRITE. Something believed it had persisted state. When that write
   vanishes with no trace, the symptom turns up somewhere else entirely and
   much later. Either let it raise, or record it (ops.capture) so it reaches
   the daily failure digest. ALLOWED_WRITE_SITES is the deliberate exception
   list: schema migrations and boot-time seeding, where "it already exists"
   IS the expected failure. Keyed by file and enclosing function. (An error.)

3. A RATCHET (scripts/silent_handlers_baseline.json): per file, the number
   of broad handlers that leave no trace, and of those the number around a
   write. "Broad" is `except Exception` / `except BaseException`, alone or
   in a tuple (`except (Exception, X)`). "No trace" is a body made only of
   `pass`, `continue`, `...`, a docstring, `return` of a constant or an
   empty container, or a `.debug(...)` log call — no capture, no call that
   reaches production's logs at all: LOG_LEVEL is INFO there, so a debug
   line is no trace (re-audit B7 P3, 10/27/26: 57 broad handlers were
   invisible in production and counted as traced). A file's counts
   may only go down: a new silent handler fails unless the same commit
   lowers another in that file. `--update` lowers the baseline to what is
   there (it refuses to raise a count without `--allow-increase`, which
   says in review that the new handler is genuinely best-effort).

A multi-statement write unit wrapped in a broad handler should be one
unit (models.atomic, A07-15): every statement is atomic on its own, so a
failure swallowed part-way commits the rest.

Run: python3 scripts/check_silent_handlers.py   (exit 1 on violations; CI)
     python3 scripts/check_silent_handlers.py --update [--allow-increase]
"""
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, "scripts", "silent_handlers_baseline.json")

# (module, function) pairs where a silent failure around a write is correct:
# the statement is a CREATE/ALTER/seed that is expected to fail once the
# schema already has it, and there is no user-visible consequence.
ALLOWED_WRITE_SITES = {
    ("rpower.py", "_mark_live"),              # the live-traffic heartbeat (R3-01): a missed beat only lets bulk run a minute sooner
    ("rpower.py", "_mark_lane"),              # the same heartbeat per lane (RPOWER audit #23 F5): a missed beat lets a lower lane borrow a minute sooner
    ("models.py", "ensure_columns"),          # ALTER TABLE ADD COLUMN, already present
    ("auth.py", "init_auth"),                 # same, for the auth tables
    ("admin_ops.py", "_load_everything"),     # CREATE TABLE IF NOT EXISTS ensure
    ("inventory.py", "get_claude_insights"),  # column ensure
    ("status_manager.py", "seed_default_services"),
    ("ops.py", "claim_period"),               # pruning old rows is opportunistic
    ("hosted_dashboard.py", "<module>"),      # boot-time PRAGMA journal_mode=WAL
    # CREATE TABLE / CREATE INDEX / ALTER for the usage ledger, run once per
    # database per process. A failure here must not break the AI call being
    # metered, and it is deliberately NOT marked done on failure — the next
    # call retries, and the insert itself heals a missing column and retries.
    ("ai_utils.py", "_ensure_usage_schema"),
}

WRITE_MARKERS = ("INSERT ", "INSERT\n", "UPDATE ", "DELETE FROM", ".commit(")

# Directories that are not the app: tests, operator scripts (run by hand,
# their own output is the trace), the iOS project, static assets, docs,
# the Cloudflare Worker, and every virtualenv or tool directory.
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".claude", "venv", ".venv", "ios", "tests", "scripts",
             "public", "static", "templates", "docs", "workers", "eval_baselines"}
_BROAD = ("Exception", "BaseException")


def _python_files():
    """Every app module, relative to ROOT, the packages included."""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(filenames):
            if f.endswith(".py") and f != "setup.py":
                yield os.path.relpath(os.path.join(dirpath, f), ROOT).replace(os.sep, "/")


def _enclosing_function(tree, lineno):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if best is None or node.lineno > best.lineno:
                    best = node
    return best.name if best else "<module>"


def is_broad(handler_type) -> bool:
    """`except Exception` / `except BaseException`, alone or in a tuple."""
    if isinstance(handler_type, ast.Name):
        return handler_type.id in _BROAD
    if isinstance(handler_type, ast.Attribute):
        return handler_type.attr in _BROAD
    if isinstance(handler_type, ast.Tuple):
        return any(is_broad(e) for e in handler_type.elts)
    return False


def _constant(value) -> bool:
    return (value is None or isinstance(value, ast.Constant)
            or (isinstance(value, (ast.List, ast.Tuple, ast.Set)) and not value.elts)
            or (isinstance(value, ast.Dict) and not value.keys))


def _debug_call(node) -> bool:
    """`<anything>.debug(...)` — a log line production's INFO level drops."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "debug")


def leaves_no_trace(body) -> bool:
    """A handler body made only of pass, continue, ..., a docstring, a
    return of a constant or an empty container, or debug log calls —
    nothing that reaches production's logs or the failure digest."""
    for stmt in body:
        if isinstance(stmt, (ast.Pass, ast.Continue)):
            continue
        if isinstance(stmt, ast.Return) and _constant(stmt.value):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if isinstance(stmt, ast.Expr) and _debug_call(stmt.value):
            continue
        return False
    return True


def _try_body_text(owner, lines):
    start = owner.body[0].lineno - 1
    end = owner.body[-1].end_lineno or owner.body[-1].lineno
    return "\n".join(lines[start:end])


def scan_file(rel_path):
    """(problems, counts) for one module: the rule 1 and 2 errors, and
    {"silent": n, "silent_writes": n} for the ratchet."""
    problems, counts = [], {"silent": 0, "silent_writes": 0}
    try:
        src = open(os.path.join(ROOT, rel_path), encoding="utf-8").read()
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"{rel_path}:{e.lineno}: could not parse ({e.msg})"], counts
    except (OSError, UnicodeDecodeError) as e:
        return [f"{rel_path}: could not read ({e})"], counts
    lines = src.splitlines()
    owners = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            for h in node.handlers:
                owners[id(h)] = node
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        if handler.type is None:
            problems.append(
                f"{rel_path}:{handler.lineno}: bare `except:` also catches "
                f"KeyboardInterrupt and SystemExit — name the exceptions, or catch Exception"
            )
            continue
        if not (is_broad(handler.type) and leaves_no_trace(handler.body)):
            continue
        counts["silent"] += 1
        owner = owners.get(id(handler))
        if owner is None or not any(m in _try_body_text(owner, lines) for m in WRITE_MARKERS):
            continue
        counts["silent_writes"] += 1
        only_pass = len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)
        if not only_pass:
            continue
        func = _enclosing_function(tree, handler.lineno)
        if (rel_path, func) in ALLOWED_WRITE_SITES:
            continue
        problems.append(
            f"{rel_path}:{handler.lineno}: `except Exception: pass` around a database write "
            f"in {func}() — let it raise, or record it with ops.capture(). "
            f"If the write is genuinely optional (a schema migration, boot seeding), "
            f"add ({rel_path!r}, {func!r}) to ALLOWED_WRITE_SITES with a reason."
        )
    return problems, counts


def check_file(rel_path):
    """Rule 1 and 2 problems for one module (the ratchet is main()'s)."""
    return scan_file(rel_path)[0]


def scan():
    """(problems, {file: counts}) over every app module; files with no
    silent handler are left out of the counts."""
    problems, counts = [], {}
    for rel in _python_files():
        p, c = scan_file(rel)
        problems.extend(p)
        if c["silent"]:
            counts[rel] = c
    return problems, counts


def load_baseline(path=None):
    try:
        with open(path or BASELINE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("files", data) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def ratchet(counts, baseline):
    """(rises, falls): [(file, key, now, allowed)] — a count above its
    baseline (a file not in the baseline is allowed none), and below it."""
    rises, falls = [], []
    for rel in sorted(set(counts) | set(baseline)):
        now = counts.get(rel) or {}
        base = baseline.get(rel) or {}
        for key in ("silent", "silent_writes"):
            n, allowed = int(now.get(key) or 0), int(base.get(key) or 0)
            if n > allowed:
                rises.append((rel, key, n, allowed))
            elif n < allowed:
                falls.append((rel, key, n, allowed))
    return rises, falls


def write_baseline(counts, path=None):
    body = {"_about": "Per-file counts of broad exception handlers that leave no trace (scripts/"
                      "check_silent_handlers.py, Audit #25 A07-08). Counts may only go down: --update lowers "
                      "them; raising one takes --allow-increase and a reason in review.",
            "total": {"silent": sum(c["silent"] for c in counts.values()),
                      "silent_writes": sum(c["silent_writes"] for c in counts.values())},
            "files": {k: counts[k] for k in sorted(counts)}}
    # One file a line, so a diff shows exactly which file's count moved.
    files = body.pop("files")
    lines = ["{"] + [f" {json.dumps(k)}: {json.dumps(v)}," for k, v in body.items()] + [' "files": {']
    lines += [f"  {json.dumps(k)}: {json.dumps(files[k], sort_keys=True)}," for k in files]
    if files:
        lines[-1] = lines[-1].rstrip(",")
    lines += [" }", "}"]
    with open(path or BASELINE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    problems, counts = scan()
    baseline = load_baseline()
    rises, falls = ratchet(counts, baseline)
    if "--update" in argv:
        if rises and "--allow-increase" not in argv:
            print("Not updated: these counts would RISE (pass --allow-increase, and say why in review):")
            for rel, key, n, allowed in rises:
                print(f"  {rel}: {key} {allowed} -> {n}")
            return 1
        write_baseline(counts)
        print(f"baseline written: {sum(c['silent'] for c in counts.values())} silent handlers in {len(counts)} files")
        return 1 if problems else 0
    for rel, key, n, allowed in rises:
        what = "silent handlers around a write" if key == "silent_writes" else "silent broad handlers"
        problems.append(f"{rel}: {n} {what} (the baseline allows {allowed}) — log or ops.capture() the new one, "
                        "narrow its exception, or let it raise (scripts/silent_handlers_baseline.json).")
    if problems:
        print("Silent-failure lint found %d problem(s):\n" % len(problems))
        for p in problems:
            print("  " + p)
        return 1
    if falls:
        print(f"silent-handler lint OK — and {len(falls)} count(s) fell below the baseline: run "
              "`python3 scripts/check_silent_handlers.py --update` to lock them in.")
        return 0
    print("silent-handler lint OK — no bare `except:`, no swallowed database writes, no new silent handlers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
