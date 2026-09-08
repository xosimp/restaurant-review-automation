#!/usr/bin/env python3
"""
check_silent_handlers.py — stop swallowed failures from creeping back in.

The pre-launch audit counted 179 `except Exception: pass` handlers. Most are
genuinely best-effort — a cache write, an optional enrichment, a notification
email that doesn't gate the thing it notifies about — and rewriting all of
them would be churn, not safety. Two shapes are not best-effort, and those
are what this lint forbids:

1. A truly bare `except:` — it catches KeyboardInterrupt and SystemExit too,
   so it can swallow a deploy's shutdown signal or a Ctrl-C. There is never a
   reason for one; name the exceptions, or catch Exception.

2. A silent handler wrapped around a database WRITE. Something believed it
   had persisted state. When that write vanishes with no trace, the symptom
   turns up somewhere else entirely and much later — a failure counter that
   never advances so a dead push token is retried forever, an activity trail
   with a hole in it, an account link that has to be redone on every sign-in.
   Either let it raise, or record it (ops.capture) so it reaches the daily
   failure digest.

ALLOWED_WRITE_SITES below is the deliberate exception list: schema migrations
and boot-time seeding, where "it already exists" IS the expected failure and
there is nothing to report. Keyed by file and enclosing function, not line
number, so it survives ordinary edits.

Run: python3 scripts/check_silent_handlers.py   (exit 1 on violations; CI)
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (module, function) pairs where a silent failure around a write is correct:
# the statement is a CREATE/ALTER/seed that is expected to fail once the
# schema already has it, and there is no user-visible consequence.
ALLOWED_WRITE_SITES = {
    ("models.py", "ensure_columns"),          # ALTER TABLE ADD COLUMN, already present
    ("auth.py", "init_auth"),                 # same, for the auth tables
    ("admin_ops.py", "_load_everything"),     # CREATE TABLE IF NOT EXISTS ensure
    ("inventory.py", "get_claude_insights"),  # column ensure
    ("status_manager.py", "seed_default_services"),
    ("ops.py", "claim_period"),               # pruning old rows is opportunistic
    ("hosted_dashboard.py", "<module>"),      # boot-time PRAGMA journal_mode=WAL
}

WRITE_MARKERS = ("INSERT ", "INSERT\n", "UPDATE ", "DELETE FROM", ".commit(")

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".claude", "venv", ".venv", "ios", "tests"}


def _python_files():
    for entry in sorted(os.listdir(ROOT)):
        if entry.endswith(".py") and entry not in ("setup.py",):
            yield entry


def _enclosing_function(tree, lineno):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if best is None or node.lineno > best.lineno:
                    best = node
    return best.name if best else "<module>"


def _owning_try(tree, handler):
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and handler in node.handlers:
            return node
    return None


def check_file(rel_path):
    problems = []
    src = open(os.path.join(ROOT, rel_path)).read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"{rel_path}:{e.lineno}: could not parse ({e.msg})"]
    lines = src.splitlines()

    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
            continue

        if handler.type is None:
            problems.append(
                f"{rel_path}:{handler.lineno}: bare `except:` also catches "
                f"KeyboardInterrupt and SystemExit — name the exceptions, or catch Exception"
            )
            continue

        broad = isinstance(handler.type, ast.Name) and handler.type.id in ("Exception", "BaseException")
        silent = len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)
        if not (broad and silent):
            continue

        owner = _owning_try(tree, handler)
        if owner is None:
            continue
        start = owner.body[0].lineno - 1
        end = owner.body[-1].end_lineno or owner.body[-1].lineno
        body = "\n".join(lines[start:end])
        if not any(marker in body for marker in WRITE_MARKERS):
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
    return problems


def main():
    problems = []
    for rel in _python_files():
        problems.extend(check_file(rel))
    if problems:
        print("Silent-failure lint found %d problem(s):\n" % len(problems))
        for p in problems:
            print("  " + p)
        return 1
    print("silent-handler lint OK — no bare `except:`, no swallowed database writes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
