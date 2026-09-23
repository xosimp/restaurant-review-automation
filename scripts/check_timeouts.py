#!/usr/bin/env python3
"""
check_timeouts.py — every outbound HTTP call names its own timeout.

The resiliency audit (Sep 2026) found three DocuSign calls with no timeout
among 46 outbound requests. On its own that is a small oversight; in this
deployment it is not.

Railway runs the app as:

    gunicorn --workers 1 --threads 4 --timeout 120

Four threads is the platform's entire concurrent request capacity. A
`requests` call with no timeout waits on the operating system's TCP
behaviour, which for a black-holed connection can be minutes. One such call
holds 25% of the platform; four hold all of it, and gunicorn's own
--timeout does not rescue a gthread worker whose threads are merely blocked
rather than wedged. The failure looks like a total outage caused by a
third party the product barely uses.

The rule is mechanical and has no exceptions worth encoding:

    A call to requests.<verb>() or httpx.<verb>() passes `timeout=`.

A session object with a default timeout would satisfy the intent but not
this check; if one is ever introduced, add it to _SESSION_FACTORIES rather
than loosening the rule.

Run: python3 scripts/check_timeouts.py   (exit 1 on violations)
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".git", "__pycache__", ".claude", "node_modules", "ios",
             "venv", ".venv", "backups", "tests"}

# Module aliases that resolve to a real HTTP client. Any other name a file
# imports one of _HTTP_PACKAGES as is added per file.
HTTP_MODULES = {"requests", "_requests", "httpx", "_httpx"}
_HTTP_PACKAGES = {"requests", "httpx"}
VERBS = {"get", "post", "put", "delete", "patch", "head", "options", "request"}

# SDK clients that make HTTP calls: constructing one must name timeout= and
# max_retries= (retrying belongs to ai_utils.create_with_retry alone).
SDK_MODULES = {"anthropic", "_anthropic"}
SDK_CLIENTS = {"Anthropic", "AsyncAnthropic"}

# If a pre-configured session/client with a baked-in timeout is ever added,
# name it here — a bare verb on it is then fine.
_SESSION_FACTORIES = set()


def _import_aliases(tree, modules):
    """Every name `import requests as X` binds, at any scope. The lint only
    knew the literal spellings, so twelve Graph calls made through
    `import requests as _req` passed it with no timeout (MOD-MKT-2)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules:
                    names.add(alias.asname or alias.name)
    return names


def offenders():
    out = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            http_names = HTTP_MODULES | _import_aliases(tree, {"requests", "httpx"})
            sdk_names = SDK_MODULES | _import_aliases(tree, {"anthropic"})
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                # An SDK client is an outbound HTTP call too: anthropic's
                # default read timeout is 600 s with 2 hidden retries, which
                # is four request threads held for ten minutes (AI-1).
                if (isinstance(fn, ast.Attribute) and fn.attr in SDK_CLIENTS
                        and isinstance(fn.value, ast.Name) and fn.value.id in sdk_names):
                    kws = {k.arg for k in node.keywords if k.arg}
                    if "timeout" not in kws or "max_retries" not in kws:
                        rel = os.path.relpath(path, ROOT)
                        out.append((rel, node.lineno, f"{fn.value.id}.{fn.attr}"))
                    continue
                if not isinstance(fn, ast.Attribute) or fn.attr not in VERBS:
                    continue
                owner = fn.value
                if not isinstance(owner, ast.Name):
                    continue
                if owner.id in _SESSION_FACTORIES:
                    continue
                if owner.id not in http_names:
                    continue
                if any(k.arg == "timeout" for k in node.keywords if k.arg):
                    continue
                # **kwargs may carry it; that is not checkable and is rare
                # enough that flagging it is the right default.
                rel = os.path.relpath(path, ROOT)
                out.append((rel, node.lineno, f"{owner.id}.{fn.attr}"))
    return out


def main():
    bad = offenders()
    if not bad:
        print("timeout lint OK — every outbound HTTP call names a timeout")
        return 0
    print(f"{len(bad)} outbound HTTP call(s) with no timeout:\n")
    for rel, lineno, call in bad:
        print(f"  {rel}:{lineno}  {call}()")
    print("\nWith --workers 1 --threads 4, one hung call is a quarter of the")
    print("platform. Pass timeout=(connect, read).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
