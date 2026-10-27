"""
scripts/test_shards.py — split the test files into N shards, deterministically
(audit #25 A01-08: the nightly Postgres suite as a matrix, not one ~2-hour
job). A file's shard is a hash of its name, so the split never depends on
the order files were added in, a new file moves nothing else, and every shard
of one run sees the same list.

    python scripts/test_shards.py --shard 1 --of 4      # one file per line
    pytest $(python scripts/test_shards.py --shard 2 --of 4) -n auto --dist loadfile
"""
import argparse
import hashlib
import os
import sys

TESTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests")


def shard_of(name, n):
    """1..n for the test file `name` (its basename)."""
    return int(hashlib.sha1(os.path.basename(name).encode("utf-8")).hexdigest(), 16) % n + 1


def test_files(root=TESTS):
    return sorted(f"tests/{n}" for n in os.listdir(root) if n.startswith("test_") and n.endswith(".py"))


def files_for(shard, n, root=TESTS):
    if not 1 <= shard <= n:
        raise ValueError(f"shard {shard} is not in 1..{n}")
    return [f for f in test_files(root) if shard_of(f, n) == shard]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, required=True)
    ap.add_argument("--of", type=int, required=True)
    args = ap.parse_args(argv)
    print("\n".join(files_for(args.shard, args.of)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
