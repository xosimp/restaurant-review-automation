"""The contract slice covers every top-10 component (audit #25 A04-01).

`pytest -m contract` is the deploy gate's slice and the pre-push habit for a
change to one of the ten components a break in would reach every restaurant
(the audit's "Change here = mandatory regression testing" table). This test
fails when a component has no contract-marked test that imports it (for
the dashboard template: reads it), so the slice cannot quietly stop covering
one — a file renamed, a marker dropped, a module split.

A file is in the slice when it carries `pytestmark = pytest.mark.contract`
(or a list holding it) at module level, or any `@pytest.mark.contract` test.
"""
import ast
import os

import pytest

pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

# Component -> the modules (or template) that are it. The order and names
# follow the audit page's section 4 table.
TOP_10 = {
    "models.get_conn / db_dialect": ["models", "db_dialect"],
    "models.get_restaurant / update_restaurant / has_module": ["models"],
    "auth.login_required / mobile_login_required": ["auth"],
    "client_api._m <-> mobile_api": ["client_api", "mobile_api"],
    "Labor % producers": ["labor", "metrics"],
    "time_utils": ["time_utils"],
    "dsr/ payloads": ["dsr"],
    "scheduler / jobs_registry / job_queue": ["scheduler", "jobs_registry", "job_queue"],
    "permissions.py": ["permissions"],
    "templates/dashboard.html": ["templates/dashboard.html"],
}


def _is_contract_mark(node):
    """`pytest.mark.contract` (bare or called)."""
    if isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Attribute) and node.attr == "contract" and \
        isinstance(node.value, ast.Attribute) and node.value.attr == "mark"


def _marked(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "pytestmark" for t in node.targets):
            vals = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            if any(_is_contract_mark(v) for v in vals):
                return True
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if any(_is_contract_mark(d) for d in node.decorator_list):
                return True
    return False


def _imports(tree):
    """Every top-level package a file imports, lazily inside a function too."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out


def contract_files():
    """{test file name: (imported top-level modules, source text)} for every
    file in the slice."""
    out = {}
    for name in sorted(os.listdir(TESTS)):
        if not (name.startswith("test_") and name.endswith(".py")) or name == os.path.basename(__file__):
            continue
        with open(os.path.join(TESTS, name), encoding="utf-8") as fh:
            text = fh.read()
        if "mark.contract" not in text:
            continue
        tree = ast.parse(text)
        if _marked(tree):
            out[name] = (_imports(tree), text)
    return out


def test_every_top_10_component_has_a_contract_test_that_reaches_it():
    files = contract_files()
    missing = []
    for component, targets in TOP_10.items():
        for target in targets:
            if target.endswith(".html"):
                hit = [f for f, (_mods, text) in files.items() if os.path.basename(target) in text]
            else:
                hit = [f for f, (mods, _text) in files.items() if target in mods]
            if not hit:
                missing.append(f"{component}: {target}")
    assert not missing, ("No contract-marked test reaches these top-10 components — mark a test that imports "
                         "them `pytestmark = pytest.mark.contract`: %s" % missing)


def test_the_deploy_gate_files_are_in_the_slice():
    # The audit's 14-file slice plus the gate's own checks; dropping a marker
    # here drops a whole contract from the gate.
    expected = {
        "test_edge_sec_permissions.py", "test_fix_d_jobs.py", "test_mem_int_wiring.py", "test_pos.py",
        "test_module_entitlement.py", "test_ios_request_body_parity.py", "test_ios_schedule_payload_contract.py",
        "test_models.py", "test_frontend_rules.py", "test_labor_ios_parity.py", "test_security_parity_audit.py",
        "test_dsr_pos_day.py", "test_dsr_end_to_end.py", "test_reaudit_c.py", "test_ask_cavnar_tools.py",
        "test_docs_controls.py", "test_resiliency.py", "test_never_production.py", "test_ci_gate_config.py",
        "test_production_config_smoke.py", "test_test_ratchets.py", "test_no_customer_data_in_repo.py",
    }
    assert expected <= set(contract_files()), sorted(expected - set(contract_files()))
