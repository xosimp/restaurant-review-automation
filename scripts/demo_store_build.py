"""
Create or rebuild Halsted & Rye, the showcase demo (demo_store), on the
database this process opens. Idempotent; touches no other restaurant.

On Railway (one plain command, from the repo root inside the container):
    railway ssh "cd /app && /opt/venv/bin/python scripts/demo_store_build.py"
Some steps only:
    ... scripts/demo_store_build.py reviews marketing

The demo login (username "halsted") takes DEMO_PASSWORD, the same variable
the older demo uses; without it the login gets a random password stored
nowhere. The nightly upkeep after this is the demo_store_refresh job.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo_store import build  # noqa: E402

if __name__ == "__main__":
    steps = tuple(sys.argv[1:]) or build.ALL_STEPS
    unknown = [s for s in steps if s not in build.ALL_STEPS]
    if unknown:
        sys.exit(f"unknown steps {unknown}; choose from {', '.join(build.ALL_STEPS)}")
    build.run_all(steps)
