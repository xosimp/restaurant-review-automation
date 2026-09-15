#!/usr/bin/env python3
"""Is the staff PIN pepper set, and is every hash written under it?

A 4-digit secret hashed without a pepper is brute-forceable offline the
moment the database leaks, so "nobody ever set the env var in production" has
to be a thing you can check in one command rather than something you discover
by reading source. See docs/PIN_PEPPER_RUNBOOK.md.

Exit status is meaningful, so this works as a deploy check:
    0  configured, and every stored hash is at the current version
    1  not configured, or hashes remain at an older version
"""
import argparse
import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from auth import pin_pepper_health  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="path to the database")
    args = ap.parse_args()

    health = pin_pepper_health(**({"db_path": args.db} if args.db else {}))
    print(f"configured:  {health['configured']}")
    print(f"pins:        {health['pins']}")
    print(f"unpeppered:  {health['unpeppered']}")
    print(f"status:      {health['message']}")
    return 0 if health["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
