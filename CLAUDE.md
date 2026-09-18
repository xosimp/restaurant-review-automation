# Cavnar AI — working instructions

Standing instructions for anyone (human or agent) working in this repo. This
file is loaded automatically at the start of every session.

## Where the architecture lives

These eight files in the repo root are the authoritative reference. Read the
relevant one before exploring code:

| File | What it covers |
|---|---|
| `CAVNAR_AI_ENGINEERING_GUIDE.md` | How the system is built and why |
| `MODULE_OVERVIEW.md` | Every module, its files, its design stance |
| `SYSTEM_ARCHITECTURE.md` | Processes, jobs, data flow |
| `DATABASE_SCHEMA.md` | Tables and their invariants |
| `API_REFERENCE.md` | Routes, web and mobile |
| `PROMPT_LIBRARY.md` | Every model call: which model, what input, what guard |
| `TESTING.md` | How the suite is organised |
| `PROJECT_CONTEXT.md` / `ROADMAP.md` | Product state and direction |

---

## CRITICAL SAFETY REQUIREMENT — code deletion is HIGH RISK

Before recommending the removal, consolidation, or replacement of any code,
service, model, component, API, utility, background job, scheduled task,
prompt, or database object, trace **all ten** of the following:

1. **Every known reference** — direct imports and calls.
2. **Indirect references** — re-exports, aliases, wrappers, base classes.
3. **Dynamic references** — `getattr`, `globals()`, string dispatch tables,
   `importlib`, anything keyed by a name built at runtime.
4. **Dependency injection usage** — registries, factories, provider maps.
5. **Scheduled and background jobs** — `scheduler.py`, cron, queues, threads.
6. **Feature flags** — module flags on `restaurants`, tier gating, env vars.
7. **Routing** — blueprints, URL rules, mobile twins of web routes.
8. **Notifications** — email, SMS, push, digests, alert types.
9. **Tests** — including tests that assert a thing is ABSENT.
10. **SwiftUI previews** — and anything else compiled but never called at
    runtime, which the compiler will still fail on.

**Only recommend removal when HIGH confidence exists that the code is truly
unused or fully superseded.**

If confidence is below High, **do not recommend deletion**. Mark the finding
instead as:

> Candidate for future cleanup after additional verification.

**Never recommend removing code based solely on the absence of obvious
references.** Favour false negatives over false positives: preserving
production stability matters more than maximising cleanup.

### Why this is stricter here than it looks

This codebase is unusually good at hiding a live reference from a grep:

- **Bound imports.** `from models import get_conn` at module top level is a
  reference that the name `models.get_conn` will never match. Several modules
  do this, and it has already caused silent wrong behaviour in tests.
- **Lazy imports inside functions.** Most cross-module calls here are
  `import x` *inside* the function body, so a module-level grep misses them.
- **Registry-driven dispatch.** `ask_cavnar_tools.TOOLS`, `pos.PROVIDERS`,
  `client_api._SETTABLE` and the `_do_*` handler lookups all reach code by
  string, not by symbol.
- **Web/mobile twins.** Almost every route exists twice (`client_api.py` and
  `mobile_api.py`); deleting one half leaves the other calling a shared body.
- **Scheduled-only callers.** A function whose only caller is a 5am job in
  `scheduler.py` looks unused from every screen in the product.
- **Tests that assert absence.** Some tests exist to prove a thing was
  deliberately removed and must stay removed — re-adding or renaming it
  breaks them in a way that reads like an unrelated failure.

### Checklist before proposing any deletion

Run these, and say in the finding what each turned up:

```bash
rg -n "\bNAME\b" --glob '!*.pyc'          # direct, including comments
rg -n "getattr|globals\(\)|importlib"     # dynamic dispatch near the call sites
rg -n "NAME" scheduler.py templates/ ios/ tests/
rg -n "NAME" --glob '*.md'                # documented as an entry point?
```

A deletion is only "verified" when the trace is written down alongside it.

---

## Other standing rules

- **Root-cause first.** Diagnose the real cause; never ship the fastest
  surface patch. Verify live before claiming something is fixed.
- **Push after every completed task.** Don't wait to be asked.
- **Restart the local backend** (`PORT=5050 python3 hosted_dashboard.py`)
  after any backend edit — the user tests against it from a real device over
  ngrok.
- **Inline JS is ES5 only**, enforced by `tests/test_frontend_rules.py`.
  Colours come from CSS variables only, enforced by `scripts/check_colors.py`.
  Every web button carries `.cbtn`, enforced by `tests/test_button_system.py`.
- **New `restaurants` column = 4 touch points**: the `Restaurant` dataclass,
  `init_db()`'s migration list, `update_restaurant()`'s `allowed` whitelist,
  and `get_restaurant()`'s hydration. Miss the whitelist and writes silently
  no-op.
- **Targeted tests by default.** Run the full suite once before pushing, or
  when asked — not after every edit.
- **`get_restaurant()` is memoised per Flask request**, and only per request —
  outside one (scheduler, tests, scripts) it is uncached, deliberately, so a
  long-running job sees rows change under it. Any new write path to
  `restaurants` outside `update_restaurant` must call
  `models._invalidate_request_cache(rid)`.
- **Never put schema DDL on a request or per-call path.** `init_db()` owns
  every table. `ai_utils._ensure_usage_schema` is the pattern where a lazy
  table is unavoidable: once per database per process, with a self-healing
  retry if a write later fails on a missing column.
- **Deployment shape.** The scheduler runs in the web process by default;
  `RUN_SCHEDULER_IN_WEB=0` plus the `worker.py` service moves it out. See
  `RAILWAY_SCHEDULER_SPLIT.md`. The single-runner guarantee is
  `ops.acquire_scheduler_lease()`, not the worker count.
- **Static assets have no cache-busting** (`/static/cavnar-orb.js`, bare
  path). Do not add a far-future `max-age` until they are hashed or
  versioned, or a JS fix will be stranded in browser caches.
