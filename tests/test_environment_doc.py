"""docs/ops/ENVIRONMENT.md names every environment variable the code reads
(docs audit, 9/30/26: the code read 268, .env.example listed 11 and 172 were
named in no doc - production's environment could not be rebuilt from the
docs). A variable added without a line there fails here."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def test_every_variable_the_code_reads_is_in_the_environment_doc():
    import repo_inventory
    doc = open(os.path.join(ROOT, "docs", "ops", "ENVIRONMENT.md"), encoding="utf-8").read()
    names = set(repo_inventory.env_vars())
    assert len(names) > 200, "the scan found almost nothing - the pattern broke"
    missing = sorted(n for n in names if f"`{n}`" not in doc)
    assert not missing, f"name these in docs/ops/ENVIRONMENT.md: {missing}"


def test_the_doc_says_how_to_regenerate_it():
    doc = open(os.path.join(ROOT, "docs", "ops", "ENVIRONMENT.md"), encoding="utf-8").read()
    assert "repo_inventory.py --env" in doc and "test_environment_doc.py" in doc
