#!/usr/bin/env python3
"""Prepare a restore out of band, so the RESTORE_FROM boot only renames.

A RESTORE_FROM boot decompresses, integrity-checks and copies the snapshot
before gunicorn binds, inside Railway's 120-second healthcheck (scale audit
10/19/26 OPS-3). Run this first, while the old process still serves: it
writes a checked, unsealed copy beside the database plus a record of it
(`<out>.prepared.json`), and never touches the live database. Then set
RESTORE_FROM to the printed path and redeploy — the boot renames the file
into place (db_restore._swap_prepared) after checking its size and mtime
still match the record.

It needs free space for one more database on the volume. An off-site copy
(.enc) needs --encrypted (it reads BACKUP_ENCRYPTION_KEY from the
environment) and the escrowed CREDENTIAL_KEY in the environment to open its
sealed credentials.

Usage (from the repo root in the container, e.g. `railway ssh`):
    python3 scripts/prepare_restore.py /app/data/backups/cavnar_ai_backup_2026-10-19.db.gz
    python3 scripts/prepare_restore.py /app/data/restore.enc --encrypted
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Prepare a snapshot for a rename-only RESTORE_FROM boot.")
    ap.add_argument("snapshot", help="a local snapshot (.db.gz or .db) or a downloaded off-site copy (.enc)")
    ap.add_argument("--out", help="where the prepared copy goes (default: restore_prepared.db beside the database)")
    ap.add_argument("--encrypted", action="store_true",
                    help="the snapshot is an off-site .enc copy; decrypt it with BACKUP_ENCRYPTION_KEY")
    args = ap.parse_args(argv)

    import db_restore
    from models import DB_PATH
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "restore_prepared.db")
    key = None
    if args.encrypted:
        key = (os.getenv("BACKUP_ENCRYPTION_KEY") or "").strip()
        if not key:
            print("BACKUP_ENCRYPTION_KEY is not set in this shell", file=sys.stderr)
            return 2
    record = db_restore.prepare(args.snapshot, out, encryption_key=key)
    print(json.dumps(record, indent=2))
    if record.get("unseal_failed"):
        print(f"WARNING: {record['unseal_failed']} sealed value(s) did not open with this CREDENTIAL_KEY and were "
              "blanked; those integrations are re-entered by hand after the restore.", file=sys.stderr)
    print(f"\nPrepared. Set RESTORE_FROM={out} on the web service and redeploy; the boot renames it into place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
