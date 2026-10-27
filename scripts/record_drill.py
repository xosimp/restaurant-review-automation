"""
record_drill.py — record one quarterly release rehearsal (release-safety audit
10/26/26, A05-05 / A05-10).

After a Railway rollback rehearsal or a Postgres point-in-time restore drill
(docs/ops/RECOVERY.md → Release drills), on production:

    railway ssh "python scripts/record_drill.py rollback --minutes 7 --note 'quiet Tuesday, clean'"
    railway ssh "python scripts/record_drill.py pg_restore --minutes 34 --note '1 restaurant count off: none'"

The Engineering system card shows the last of each and warns past 100 days.
Writes one release_drills row; sends nothing.
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    sys.path.insert(0, _ROOT)
    import platform_monitor
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("kind", choices=sorted(platform_monitor.DRILL_KINDS))
    ap.add_argument("--minutes", type=float, default=None, help="how long it took, start to healthy")
    ap.add_argument("--failed", action="store_true", help="the drill did not get back to healthy")
    ap.add_argument("--note", default=None)
    ap.add_argument("--by", default=os.getenv("USER") or None)
    args = ap.parse_args(argv)
    rid = platform_monitor.record_drill(args.kind, minutes=args.minutes, ok=not args.failed, note=args.note,
                                        recorded_by=args.by)
    print(f"recorded {args.kind} drill #{rid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
