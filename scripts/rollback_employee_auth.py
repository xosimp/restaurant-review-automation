#!/usr/bin/env python3
"""Roll the employee-auth tier back out of a live database.

The employee tier was built additively — new tables, new columns, nothing
dropped or rewritten — so "rollback" is mostly "stop reading it". That is a
true statement about the schema and a useless one at 2am, because the part
that actually matters is the part additive changes do NOT cover: the live
staff sessions, the portal links people have saved, and the PIN hashes.

This script does the operational rollback, in the order you would want it:

  1. Ends every live staff session      (nobody is left signed in to a portal
                                         the app no longer serves)
  2. Revokes every staff portal token   (saved links stop resolving)
  3. Clears PIN hashes                  (optional; --keep-pins by default,
                                         because a rollback you intend to undo
                                         should not force every restaurant to
                                         reissue forty PINs)
  4. Leaves memberships intact          (they are what console roles now read
                                         through; deleting them would demote
                                         every owner to the users.role
                                         fallback, which is a bigger change
                                         than the rollback)

Nothing here drops a table or a column. SQLite cannot drop a column without
rebuilding the table, and a rollback that rebuilds `restaurants` under load is
a worse outcome than the thing being rolled back.

Usage:
    python3 scripts/rollback_employee_auth.py --dry-run
    python3 scripts/rollback_employee_auth.py --confirm
    python3 scripts/rollback_employee_auth.py --confirm --clear-pins
"""
import argparse
import sqlite3
import sys
from datetime import datetime


def _counts(conn):
    def one(sql, *args):
        try:
            row = conn.execute(sql, args).fetchone()
            return row[0] if row else 0
        except sqlite3.Error:
            return 0
    return {
        "staff sessions": one("SELECT COUNT(*) FROM sessions WHERE device_type='staff_pin'"),
        "live portal tokens": one("SELECT COUNT(*) FROM staff_portal_tokens WHERE revoked_at IS NULL"),
        "memberships with a PIN": one("SELECT COUNT(*) FROM memberships WHERE pin_hash IS NOT NULL"),
        "employee memberships": one("SELECT COUNT(*) FROM memberships WHERE role='employee'"),
        "console memberships": one("SELECT COUNT(*) FROM memberships WHERE role<>'employee'"),
        "pending portal nonces": one("SELECT COUNT(*) FROM portal_nonces"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="reviews.db", help="path to the database")
    ap.add_argument("--confirm", action="store_true", help="actually write")
    ap.add_argument("--dry-run", action="store_true", help="report only (default)")
    ap.add_argument("--clear-pins", action="store_true",
                    help="also null out pin_hash — every PIN must be reissued")
    args = ap.parse_args()

    if not args.confirm:
        args.dry_run = True

    conn = sqlite3.connect(args.db)
    before = _counts(conn)
    print(f"Database: {args.db}")
    print(f"Time:     {datetime.now().isoformat(timespec='seconds')}")
    print("\nCurrent state:")
    for k, v in before.items():
        print(f"  {v:>6}  {k}")

    if args.dry_run:
        print("\nDRY RUN — nothing written. Re-run with --confirm to apply.")
        print("Would: end staff sessions, revoke portal tokens, clear nonces"
              + (", clear PIN hashes" if args.clear_pins else ""))
        print("Would NOT: touch memberships, drop any table, drop any column.")
        conn.close()
        return 0

    try:
        conn.execute("DELETE FROM sessions WHERE device_type='staff_pin'")
        conn.execute("UPDATE staff_portal_tokens SET revoked_at=datetime('now') "
                     "WHERE revoked_at IS NULL")
        conn.execute("DELETE FROM portal_nonces")
        conn.execute("DELETE FROM portal_attempts")
        conn.execute("DELETE FROM membership_pin_attempts")
        if args.clear_pins:
            conn.execute("UPDATE memberships SET pin_hash=NULL, pin_set_at=NULL, "
                         "updated_at=datetime('now') WHERE pin_hash IS NOT NULL")
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        print(f"\nFAILED: {exc}", file=sys.stderr)
        conn.close()
        return 1

    after = _counts(conn)
    conn.close()
    print("\nAfter:")
    for k, v in after.items():
        print(f"  {v:>6}  {k}")
    print("\nDone. Memberships were left intact — console roles read through them,")
    print("so removing them would demote every owner to the users.role fallback.")
    if not args.clear_pins:
        print("PIN hashes kept: re-enabling the tier needs no PIN reissue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
