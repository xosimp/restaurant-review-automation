"""
build_reference_schema.py — /health's reference schema, built at image-build
time (release-safety audit 10/26/26, A05-09).

Run by the Nixpacks build phase (nixpacks.toml). It boots the whole boot
schema (models.boot_schema) into a scratch SQLite file and writes
reference_schema.json beside the code, stamped with a hash of the code it
was built from. status_manager._reference_columns reads it instead of
paying a full scratch boot inside every deploy's outage; when the file is
missing, unreadable or from other code, the process builds the reference
itself, as before.

The build's environment is scrubbed first: no DATABASE_URL (a scratch boot
must never reach production's Postgres — a build runs while the old
container serves), a throwaway volume, no provider keys. Exit status is
informational: nixpacks.toml never lets this fail a build.

    python scripts/build_reference_schema.py [--out PATH]
"""
import argparse
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# Anything that could reach a real system from a scratch boot.
_SCRUB_PREFIXES = ("DATABASE_URL", "PG", "RESEND_", "TWILIO_", "STRIPE_", "DOCUSIGN_", "ANTHROPIC_", "APNS_",
                   "GOOGLE_", "PERPLEXITY_", "SENTRY_", "OPS_PAGER", "R2_", "S3_", "BACKUP_", "CREDENTIAL_KEY",
                   "RESTORE_FROM")


def scrub_environment(env=None, volume=None):
    """The environment a scratch boot runs in: provider keys and DATABASE_URL
    removed, SQLite pinned, the volume a fresh temporary directory."""
    env = os.environ if env is None else env
    for key in list(env):
        if key.startswith(_SCRUB_PREFIXES):
            env.pop(key, None)
    env["DB_BACKEND"] = "sqlite"
    env["RUN_SCHEDULER_IN_WEB"] = "0"
    env["RAILWAY_VOLUME_MOUNT_PATH"] = volume or tempfile.mkdtemp(prefix="cavnar-reference-volume-")
    return env


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=None, help="where to write (default: reference_schema.json beside the code)")
    args = ap.parse_args(argv)
    scrub_environment()
    sys.path.insert(0, _ROOT)
    started = time.monotonic()
    import status_manager
    path = status_manager.write_reference_artifact(path=args.out)
    print(f"reference schema written to {path} in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:          # informational: the runtime builds it instead
        print(f"reference schema not prebuilt ({e}); the app builds it at runtime")
        sys.exit(1)
