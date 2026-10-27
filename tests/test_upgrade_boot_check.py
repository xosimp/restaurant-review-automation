"""Blind re-audit 10/27/26 (RF6, B5 P1) — scripts/upgrade_boot_check.py boots
this code on the previous release's database, seeded, and fails on what a
fresh boot cannot see: a new UNIQUE index over existing rows, an index built
at boot on a table the previous release had, a required column added to a
populated table (Postgres), a boot statement or run-once migration that
failed.

The end-to-end tests boot real processes (~15-30 s each); the Postgres ones
run when CAVNAR_TEST_PG_URL names a scratch server."""
import contextlib
import io
import os
import shutil

import pytest

import models
from scripts import upgrade_boot_check as ubc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PG = (os.getenv("CAVNAR_TEST_PG_URL") or "").strip()


def _git_ok():
    return shutil.which("git") is not None and os.path.exists(os.path.join(ROOT, ".git"))


needs_git = pytest.mark.skipif(not _git_ok(), reason="no git checkout to archive the previous release from")


# ── the pieces ───────────────────────────────────────────────────────────────

def test_the_seed_fills_nearly_every_table(tmp_path):
    path = str(tmp_path / "seed.db")
    with contextlib.redirect_stdout(io.StringIO()):
        models.boot_schema(path)
    db = ubc._Db(sqlite_path=path)
    try:
        out = ubc.seed(db)
        assert out["seeded"] >= 0.97 * out["tables"], out["unseeded"]
        for table in ("restaurants", "users", "sessions", "memberships", "login_history", "reviews",
                      "ai_usage", "jobs", "guest_contacts"):
            assert table not in out["unseeded"], table
        # The legacy shapes the boot's run-once fixes exist for.
        assert db.scalar("SELECT expires_at FROM sessions WHERE token='seed-session-iso'") == \
            "2030-01-01T10:00:00+00:00"
        assert db.scalar("SELECT username FROM users WHERE email='pin1@staff.invalid'") == "SeedPin"
    finally:
        db.close()


def test_synthetic_values_are_shaped_by_the_column():
    ids = ubc.IDS
    assert ubc.synthetic_value("t", "restaurant_id", "INTEGER", 1, ids) == ids["rid"]
    assert ubc.synthetic_value("t", "is_active", "INTEGER", 1, ids) == 1
    assert ubc.synthetic_value("t", "rules_json", "TEXT", 1, ids) == "{}"
    assert ubc.synthetic_value("t", "created_at", "TEXT", 2, ids) == "2026-10-21 12:00:00"
    assert ubc.synthetic_value("t", "business_date", "TEXT", 1, ids) == "2026-10-20"
    assert ubc.synthetic_value("t", "timezone", "TEXT", 1, ids) == "America/Chicago"
    a, b = (ubc.synthetic_value("t", "label", "TEXT", n, ids) for n in (1, 2))
    assert a != b


def test_what_fails_a_boot():
    clean = {"booted": True, "status": 200, "health": {"problems": ["scheduler_stale"]}, "schema_gaps": [],
             "missing_indexes": {}, "records": [["WARNING", "x", "nothing to see"]], "printed": ["DB init OK"]}
    assert ubc.boot_failures(clean) == [], "an environmental problem is not the upgrade's"
    assert ubc.boot_failures({"booted": False, "boot_error": "RuntimeError('x')"}) == \
        ["did not boot: RuntimeError('x')"]
    bad = dict(clean, status=500, schema_gaps=["reviews.x"], missing_indexes={"uq_x": {"unique": True, "error": "d"}},
               records=[["WARNING", "models", "[m1] run-once data migration failed, retried next boot: boom"]],
               printed=["[auth] index creation failed: locked"])
    fails = ubc.boot_failures(bad)
    assert any("/health answered 500" in f for f in fails)
    assert any("schema gaps" in f for f in fails)
    assert any("index uq_x not built (unique)" in f for f in fails)
    assert sum("boot statement failed" in f for f in fails) == 2
    # The previous release's own boot is held to booting and answering 200.
    assert ubc.boot_failures(bad, base=True) == [f for f in fails if "/health answered" in f]


@needs_git
def test_the_base_ref_resolves():
    head = ubc._git(ROOT, "rev-parse", "HEAD")
    assert ubc.resolve_base_ref(ROOT, "HEAD") == head
    base = ubc.resolve_base_ref(ROOT)                     # origin/production, the merge-base, or HEAD~1
    assert len(base) == 40 and base != head or ubc._git(ROOT, "rev-parse", "--verify", "--quiet",
                                                        "origin/production", check=False)
    with pytest.raises(RuntimeError):
        ubc.resolve_base_ref(ROOT, "no-such-ref-rf6")


def test_a_remote_database_url_is_refused():
    assert ubc.main(["--database-url", "postgresql://u@db.example.com/postgres", "--base-root", ROOT]) == 2


# ── end to end ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def base_tree(tmp_path_factory):
    if not _git_ok():
        pytest.skip("no git checkout to archive the previous release from")
    return ubc.extract_tree(ROOT, "HEAD", str(tmp_path_factory.mktemp("rf6-base") / "tree"))


def _patched(base_tree, tmp_path, line):
    """A copy of the tree whose boot runs `line` after its own boot schema."""
    head = str(tmp_path / "head")
    shutil.copytree(base_tree, head)
    p = os.path.join(head, "models.py")
    src = open(p, encoding="utf-8").read()
    anchor = "    sales_audits.init_sales_audits(db_path=path)\n"
    assert src.count(anchor) == 1, "the boot schema's last step moved; update this test's anchor"
    open(p, "w", encoding="utf-8").write(src.replace(anchor, anchor + "    " + line + "\n"))
    return head


def test_an_upgrade_from_the_committed_tree_to_this_one_passes(base_tree):
    out = ubc.run_backend(base_tree, ROOT, None, rollback=True, out=lambda *_: None)
    assert out["ok"], out["failures"]
    assert [s["step"].split(" (")[0] for s in out["steps"]] == [
        "base boot", "base boot again", "upgrade boot", "second boot", "rollback boot"]


def test_a_new_unique_index_over_existing_rows_fails_the_upgrade_check(base_tree, tmp_path):
    """login_history's seeded rows share a restaurant: a fresh boot builds
    the unique index without a murmur; the upgrade boot cannot. And a plain
    index it does build on that existing table breaks the prebuilt rule —
    judged against the previous release itself, not a hand-kept baseline."""
    head = _patched(base_tree, tmp_path,
                    '_c = boot_conn(path); create_unique_index(_c, "CREATE UNIQUE INDEX IF NOT EXISTS uq_rf6_probe '
                    'ON login_history(restaurant_id)"); _c.execute("CREATE INDEX IF NOT EXISTS idx_rf6_probe ON '
                    'login_history(created_at, user_id)"); _c.commit(); _c.close()')
    out = ubc.run_backend(base_tree, head, None, rollback=False, out=lambda *_: None)
    assert not out["ok"] and not out["inconclusive"]
    text = "\n".join(out["failures"])
    assert "index uq_rf6_probe not built (unique)" in text
    assert "index rule: idx_rf6_probe built at boot on login_history" in text


@pytest.mark.skipif(not PG, reason="CAVNAR_TEST_PG_URL names no scratch Postgres")
def test_a_required_column_on_a_populated_table_fails_only_the_upgrade_on_postgres(base_tree, tmp_path):
    head = _patched(base_tree, tmp_path, '_c = boot_conn(path); _c.execute("ALTER TABLE login_history ADD '
                                         'COLUMN rf6_required TEXT NOT NULL"); _c.commit(); _c.close()')
    out = ubc.run_backend(base_tree, head, PG, rollback=False, out=lambda *_: None)
    assert not out["ok"] and not out["inconclusive"]
    assert any(f.startswith("upgrade: did not boot") for f in out["failures"]), out["failures"]
    # ...while the same code on an empty database boots: what the gate's fresh boot saw.
    import psycopg
    import secrets
    name = f"rf6_fresh_{secrets.token_hex(4)}"
    with psycopg.connect(PG, autocommit=True, connect_timeout=10) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        vol = tmp_path / "fresh"
        vol.mkdir()
        (vol / "reviews.db").touch()
        rep = ubc.boot(head, str(vol), ubc._pg_database(PG, name))
        assert rep.get("booted") and rep.get("status") == 200, rep.get("tail")
    finally:
        with psycopg.connect(PG, autocommit=True, connect_timeout=10) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
