#!/usr/bin/env python3
"""The pre-shift thundering herd: does a PIN sign-in still resolve at scale?

The employee tier changes the `sessions` access pattern from roughly one row
per restaurant to one row per employee per shift, and every sign-in runs two
DELETE … WHERE user_id=? statements plus a session resolve. That is fine at
six accounts and it is the first thing that falls over at 250,000 — and it
falls over at the worst possible moment, when a whole kitchen signs in inside
the same ninety seconds.

This builds a database of that shape and measures it, rather than reasoning
about it. It deliberately measures the DATA layer, not HTTP: the question is
whether the query plan holds, and a local Flask test client would bury that
under request overhead.

    python3 scripts/loadtest_staff_signin.py                  # 100 × 40
    python3 scripts/loadtest_staff_signin.py --restaurants 500 --staff 40
    python3 scripts/loadtest_staff_signin.py --no-index       # the old shape

--no-index drops the index this audit added, so the two runs can be compared
directly. That comparison is the point: it is what turns "add an index" from
an assertion into a measurement.
"""
import argparse
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

import auth  # noqa: E402
import models  # noqa: E402


def _pct(values, p):
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(len(ordered) * p / 100.0))
    return ordered[idx]


def build(db_path, restaurants, staff, verbose=True):
    models.init_db(db_path)
    auth.init_auth(db_path)
    made = []
    for r in range(restaurants):
        rid = models.create_restaurant(
            models.Restaurant(name=f"Load {r}", owner_email=f"owner{r}@load.test"),
            db_path=db_path)
        token = auth.get_or_create_staff_portal_token(rid, db_path=db_path)
        people = []
        for s in range(staff):
            uid = auth.create_user(rid, f"emp{r}.{s}", f"emp{r}.{s}@load.test",
                                   "unused", db_path=db_path)
            m = auth.upsert_membership(uid, rid, "employee",
                                       employee_name=f"Emp {r}.{s}", db_path=db_path)
            auth.set_membership_pin(m["id"], rid, "5063", db_path=db_path)
            people.append((uid, m["id"]))
        made.append((rid, token, people))
        if verbose and (r + 1) % 25 == 0:
            print(f"  seeded {r + 1}/{restaurants} restaurants…", flush=True)
    return made


def herd(db_path, made, restaurants_in_rush):
    """One restaurant's whole staff signing in, repeated across restaurants."""
    verify_ms, session_ms, resolve_ms = [], [], []
    for rid, _token, people in made[:restaurants_in_rush]:
        for uid, mid in people:
            t0 = time.perf_counter()
            result = auth.verify_membership_pin(mid, rid, "5063", db_path=db_path)
            t1 = time.perf_counter()
            assert result["ok"], result
            token = auth.create_staff_session(uid, rid, db_path=db_path)
            t2 = time.perf_counter()
            user = auth.get_session_user(token, db_path=db_path)
            t3 = time.perf_counter()
            assert user and user["membership_id"] == mid
            verify_ms.append((t1 - t0) * 1000)
            session_ms.append((t2 - t1) * 1000)
            resolve_ms.append((t3 - t2) * 1000)
    return verify_ms, session_ms, resolve_ms


def report(label, values):
    print(f"  {label:<26} median {statistics.median(values):7.2f} ms   "
          f"p95 {_pct(values, 95):7.2f} ms   max {max(values):7.2f} ms")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--restaurants", type=int, default=100)
    ap.add_argument("--staff", type=int, default=40)
    ap.add_argument("--rush", type=int, default=5,
                    help="how many restaurants' staff to time (all are seeded)")
    ap.add_argument("--no-index", action="store_true",
                    help="drop idx_sessions_user first, to measure the old shape")
    args = ap.parse_args()

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="cavnar-load-")
    os.close(fd)
    os.unlink(db_path)
    try:
        total = args.restaurants * args.staff
        print(f"Seeding {args.restaurants} restaurants × {args.staff} staff "
              f"= {total} employee accounts…")
        t0 = time.perf_counter()
        made = build(db_path, args.restaurants, args.staff)
        print(f"Seeded in {time.perf_counter() - t0:.1f}s\n")

        if args.no_index:
            conn = models.get_conn(db_path)
            conn.execute("DROP INDEX IF EXISTS idx_sessions_user")
            conn.execute("DROP INDEX IF EXISTS idx_sessions_expires")
            conn.commit()
            conn.close()
            print("Dropped idx_sessions_user / idx_sessions_expires "
                  "— measuring the PRE-AUDIT shape\n")

        conn = models.get_conn(db_path)
        sessions = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        plan = conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM sessions WHERE user_id=1 "
            "AND expires_at <= datetime('now')").fetchall()
        conn.close()
        print(f"sessions rows before the rush: {sessions}")
        for row in plan:
            print(f"  query plan: {row[-1]}")
        print()

        signing_in = min(args.rush, args.restaurants) * args.staff
        print(f"Timing {signing_in} sign-ins (verify → create session → resolve):")
        verify_ms, session_ms, resolve_ms = herd(db_path, made, args.rush)
        report("PIN verify (scrypt)", verify_ms)
        report("create_staff_session", session_ms)
        report("get_session_user", resolve_ms)

        per_signin = (statistics.median(verify_ms) + statistics.median(session_ms)
                      + statistics.median(resolve_ms))
        print(f"\n  end to end, median: {per_signin:.2f} ms per sign-in")
        print(f"  40 staff inside 90s at one restaurant needs "
              f"{40 * per_signin / 1000:.2f}s of database time — "
              f"{'comfortable' if 40 * per_signin < 90000 else 'TOO SLOW'}")
        conn = models.get_conn(db_path)
        after = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        conn.close()
        print(f"  sessions rows after: {after}")
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
