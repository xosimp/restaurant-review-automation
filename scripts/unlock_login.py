#!/usr/bin/env python3
"""Break-glass: clear a login's sign-in lockout from a shell on the box.

For the day the only admin is locked out of the console that has the
"Clear lockout" button (SECURITY-12). It clears the account's lock and every
per-address lock on it (security.clear_account_lock) — nothing else: no
password, no session, no two-factor is touched — and records the unlock in
admin_events.

The other break-glass is LOGIN_UNLOCK_USERNAMES=<name>[,<name>] in Railway:
set it, redeploy, unset it (security.apply_boot_unlocks).

Usage (from the repo root on the running service, e.g. `railway ssh`):
    python3 scripts/unlock_login.py will
    python3 scripts/unlock_login.py will --dry-run
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Clear a login's sign-in lockout.")
    ap.add_argument("username", help="the login's username (or the email it was typed as)")
    ap.add_argument("--dry-run", action="store_true", help="show the lock, change nothing")
    args = ap.parse_args(argv)

    import security
    name = args.username.strip().lower()
    state = security.lockout_state(name)
    print(f"{name}: locked={state['locked']} seconds_left={state['seconds_left']} "
          f"failures_15m={state['failures_15m']} failures_24h={state['failures_24h']} "
          f"addresses_locked={sum(1 for a in state['addresses'] if a['locked'])}")
    if args.dry_run:
        return 0
    removed = security.clear_account_lock(name)
    try:
        import admin_events
        admin_events.record("admin", "lockout_cleared_break_glass",
                            summary=f"scripts/unlock_login.py cleared the sign-in lock on {name}",
                            payload={"username": name, "rows": removed})
    except Exception:
        pass
    print(f"{name}: cleared {removed} failure row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
