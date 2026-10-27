"""A test that boots hosted_dashboard in a subprocess must stub dotenv first
(release-safety audit #25, WS9 finding). The in-process suite is covered by
conftest; a subprocess is not, and hosted_dashboard loads the .env beside it —
in the main checkout, production's keys."""
import glob
import os
import re

HERE = os.path.dirname(__file__)


def test_every_subprocess_app_stubs_load_dotenv():
    missing = []
    for path in sorted(glob.glob(os.path.join(HERE, "**", "*.py"), recursive=True)):
        src = open(path, encoding="utf-8").read()
        boots = re.search(r"^(import hosted_dashboard|from hosted_dashboard)", src, re.M)
        if "subprocess" in src and boots and "load_dotenv" not in src:
            missing.append(os.path.relpath(path, HERE))
    assert missing == [], ("stub dotenv before importing hosted_dashboard in the subprocess script "
                           f"(`import dotenv; dotenv.load_dotenv = lambda *a, **k: False`): {missing}")
