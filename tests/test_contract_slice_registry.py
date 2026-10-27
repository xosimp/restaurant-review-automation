"""The contract slice covers every top component with a test that exercises
it (audit #25 A04-01; blind re-audit 10/27/26, B1 F5 and B4 "the top-10
registry counts an import as coverage").

`pytest -m contract` is the deploy gate's slice and the pre-push habit for a
change to a component a break in would reach every restaurant. The registry
below names, for each component, the modules that ARE it and the contract
tests that guard it — explicitly, file by file (or test by test), not "some
marked file happens to import the module". It used to be the latter, and
"time_utils" was then covered by a test that monkeypatched
time_utils.restaurant_now, and "metrics" by an unused `import metrics  #
noqa: F401`.

This file fails when:
- a registered test file is missing, or a registered test no longer exists;
- a registered file (or test) has lost its contract mark — dropping a
  marker would drop a whole net from the gate;
- no registered test of a component EXERCISES one of its modules: calls one
  of its functions or reads one of its attributes (a blueprint, a constant).
  An import alone, or a reference that only hands the module to
  monkeypatch.setattr / mock.patch.object, is not coverage; nor is a call to
  an attribute the same file replaces. For the dashboard template, the test
  must read the file.

A file is in the slice when it carries `pytestmark = pytest.mark.contract`
(or a list holding it) at module level; a single test is when it (or its
class) is decorated `@pytest.mark.contract`.
"""
import ast
import os

import pytest

pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

# component -> {"modules": what it is, "tests": what guards it in the gate}.
# The first ten follow the audit's "change here = mandatory regression
# testing" table; the rest are the single points of failure the blind
# re-audit added (config's send gate, ops, the model gateway, the send
# points, the blueprint/CSRF plan, the installed-build gate, tenant
# isolation, the manager rule, rollback).
REGISTRY = {
    "models.get_conn / db_dialect": {
        "modules": ["models", "db_dialect"],
        "tests": ["test_models.py", "test_never_production.py", "test_no_bound_get_conn.py"]},
    "models.get_restaurant / update_restaurant / has_module": {
        "modules": ["models"],
        "tests": ["test_models.py", "test_module_entitlement.py"]},
    "models.boot_schema / boot DDL rules": {
        "modules": ["models"],
        "tests": ["test_ws2_boot_schema.py", "test_boot_ddl_rules.py"]},
    "auth.login_required / mobile_login_required / view-as": {
        "modules": ["auth"],
        "tests": ["test_edge_sec_permissions.py", "test_real_app_permissions.py", "test_twin_wiring.py"]},
    "client_api._m <-> mobile_api": {
        "modules": ["client_api", "mobile_api"],
        "tests": ["test_twin_wiring.py", "test_ios_response_shapes.py", "test_tenant_isolation.py"]},
    "Labor % producers (one figure, salary views)": {
        "modules": ["labor", "metrics"],
        "tests": ["test_labor_pct_one_figure.py", "test_reaudit_1020_salary_views.py", "test_labor_ios_parity.py",
                  "test_audit25_ws3_financial.py"]},
    "time_utils": {
        "modules": ["time_utils"],
        "tests": ["test_time_utils.py"]},
    "dsr/ payloads": {
        "modules": ["dsr"],
        "tests": ["test_dsr_pos_day.py", "test_dsr_end_to_end.py"]},
    "scheduler / jobs_registry / job_queue": {
        "modules": ["scheduler", "jobs_registry", "job_queue"],
        "tests": ["test_fix_d_jobs.py", "test_scheduler_drive.py", "test_job_queue_handlers.py",
                  "test_sf11_q1_queue.py"]},
    "permissions.py": {
        "modules": ["permissions"],
        "tests": ["test_permissions.py", "test_real_app_permissions.py", "test_edge_sec_permissions.py"]},
    "templates/dashboard.html": {
        "modules": ["templates/dashboard.html"],
        "tests": ["test_frontend_rules.py", "test_client_paths_resolve.py"]},
    "config: deploy_env / outbound_sends_allowed / scheduling_allowed": {
        "modules": ["config"],
        "tests": ["test_ws2_staging_gate.py", "test_never_production.py"]},
    "ops: capture / run_job / claim_cooldown / the lease": {
        "modules": ["ops"],
        "tests": ["test_ops.py", "test_fix_d_ops.py"]},
    "ai_utils / model_gateway": {
        "modules": ["ai_utils", "model_gateway"],
        "tests": ["test_ai_utils.py", "test_sf10_h2_gateway.py"]},
    "send points: emails / notify / push": {
        "modules": ["emails", "notify", "push"],
        "tests": ["test_email_retry_after.py", "test_sms_uncertain_outcome.py", "test_push_transient_reasons.py",
                  "test_owner_jobs_send_once.py", "test_vendor_signature_vectors.py"]},
    "blueprints / csrf (the registration plan)": {
        "modules": ["blueprints", "csrf"],
        "tests": ["test_cross_tenant_crawl.py", "test_client_paths_resolve.py", "test_csrf.py"]},
    "client_compat (the installed-build gate)": {
        "modules": ["client_compat"],
        "tests": ["test_client_compat.py"]},
    "tenant isolation (every route; the per-restaurant keys)": {
        "modules": ["blueprints", "models"],
        "tests": ["test_cross_tenant_crawl.py", "test_tenant_isolation.py"]},
    "the manager-every-minute rule (schedule rules, publish, requests)": {
        "modules": ["schedule_rules", "schedule_publish", "shift_requests"],
        "tests": ["test_manager_every_minute.py", "test_schedule_publish_gate.py", "test_schedule_notice_outbox.py",
                  "test_schedule_rules_reaudit_1004.py::test_approving_the_only_managers_drop_says_the_floor_goes_unmanaged",
                  "test_sf11_l_lifecycle.py::test_the_only_managers_drop_needs_who_runs_the_floor_and_the_gate_reads_the_gap",
                  "test_sf10_d2_requests.py::test_a_swap_that_takes_the_only_manager_off_saturday_is_refused",
                  "test_sf10_d2_requests.py::test_a_drop_that_takes_the_manager_off_never_approves_itself",
                  "test_solver_behaviour.py::test_one_random_restaurant_drafted_once_is_legal_with_a_manager_on_every_minute"]},
    "rollback safety and switches": {
        "modules": ["platform_monitor", "models"],
        "tests": ["test_ws2_rollback_safety.py", "test_rollback_switches.py"]},
    "providers: recorded corpus, Stripe's real SDK, signatures": {
        "modules": ["webhook_routes", "billing_jobs"],
        "tests": ["test_provider_recorded_payloads.py", "test_stripe_real_sdk.py", "test_vendor_signature_vectors.py"]},
}

# Files in the slice that no component above lists (the gate's own checks,
# client contracts, ledgers): a dropped marker on one of these drops a net
# from the gate just the same.
ALSO_IN_THE_SLICE = {
    "test_mem_int_wiring.py", "test_pos.py", "test_ios_request_body_parity.py", "test_ios_schedule_payload_contract.py",
    "test_security_parity_audit.py", "test_reaudit_c.py", "test_ask_cavnar_tools.py", "test_docs_controls.py",
    "test_resiliency.py", "test_ci_gate_config.py", "test_production_config_smoke.py", "test_test_ratchets.py",
    "test_no_customer_data_in_repo.py", "test_client_compat.py", "test_client_paths_resolve.py",
    "test_stale_tab_reload.py", "test_tock_event_order.py", "test_value_delivered.py", "test_process_caches.py",
    "test_ws2_staging_gate.py", "test_boot_check.py", "test_subprocess_apps_never_load_dotenv.py",
    "test_promote.py",
}


def _is_contract_mark(node):
    """`pytest.mark.contract` (bare or called)."""
    if isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Attribute) and node.attr == "contract" and \
        isinstance(node.value, ast.Attribute) and node.value.attr == "mark"


def _module_marked(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "pytestmark" for t in node.targets):
            vals = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            if any(_is_contract_mark(v) for v in vals):
                return True
    return False


def _find(tree, dotted):
    """The function (or class) node named `Class::test` / `test` at module
    level, with the chain of enclosing nodes; None when absent."""
    parts = dotted.split("::")
    scope, chain = tree.body, []
    for part in parts:
        hit = next((n for n in scope if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and n.name == part), None)
        if hit is None:
            return None, chain
        chain.append(hit)
        scope = hit.body
    return chain[-1], chain


def _load(name):
    path = os.path.join(TESTS, name)
    if not os.path.exists(path):
        return None, None
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return ast.parse(text), text


def _in_slice(entry):
    """(present, marked, the node to scan for use) for 'file.py' or
    'file.py::test'."""
    fname, _, test = entry.partition("::")
    tree, text = _load(fname)
    if tree is None:
        return False, False, None, None
    if not test:
        marked = _module_marked(tree) or any(
            _is_contract_mark(d) for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for d in n.decorator_list)
        # A file listed whole must carry the module-level mark.
        return True, _module_marked(tree), tree, text
    node, chain = _find(tree, test)
    if node is None:
        return False, False, None, None
    marked = _module_marked(tree) or any(_is_contract_mark(d) for n in chain for d in n.decorator_list)
    return True, marked, node, text


def _aliases(tree, module):
    """(names bound to `module`, names imported from it) anywhere in the file
    — lazy imports inside a function body too."""
    mods, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if a.name == module or (top == module and a.asname is None):
                    mods.add(a.asname or top)
                elif a.name.startswith(module + ".") and a.asname:
                    mods.add(a.asname)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module == module or node.module.startswith(module + "."):
                names |= {a.asname or a.name for a in node.names}
            else:
                for a in node.names:
                    if f"{node.module}.{a.name}" == module or (node.module == module.rsplit(".", 1)[0]
                                                               and a.name == module.rsplit(".", 1)[-1]):
                        mods.add(a.asname or a.name)
    return mods, names


def _patched_attrs(tree, mods):
    """Attributes of the module the file replaces with monkeypatch.setattr /
    mock.patch.object(<module>, "<name>", …)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("setattr", "object") and len(node.args) >= 2 \
                and isinstance(node.args[0], ast.Name) and node.args[0].id in mods \
                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            out.add(node.args[1].value)
    return out


def _root_name(attr):
    while isinstance(attr, ast.Attribute):
        attr = attr.value
    return attr.id if isinstance(attr, ast.Name) else None


def exercises(tree, scope, module):
    """Whether `scope` (a file's tree, or one test function) calls a function
    of `module` or reads one of its attributes — not only imports it, and
    not only through an attribute this same file replaced."""
    mods, names = _aliases(tree, module)
    patched = _patched_attrs(tree, mods)
    for node in ast.walk(scope):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and _root_name(node) in mods:
            first = node
            while isinstance(first.value, ast.Attribute):
                first = first.value
            if first.attr not in patched:
                return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names \
                and node.func.id not in patched:
            return True
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names \
                and node.id not in patched:
            return True
    return False


def test_every_registered_test_exists_and_is_in_the_slice():
    problems = []
    for component, spec in REGISTRY.items():
        assert spec["tests"] and spec["modules"], component
        for entry in spec["tests"]:
            present, marked, _node, _text = _in_slice(entry)
            if not present:
                problems.append(f"{component}: {entry} does not exist")
            elif not marked:
                problems.append(f"{component}: {entry} is not marked contract (pytestmark = pytest.mark.contract "
                                "at module level, or @pytest.mark.contract on the test)")
    assert not problems, "\n".join(problems)


def test_every_component_is_exercised_by_one_of_its_contract_tests():
    missing = []
    for component, spec in REGISTRY.items():
        for module in spec["modules"]:
            hit = []
            for entry in spec["tests"]:
                present, marked, node, text = _in_slice(entry)
                if not (present and marked):
                    continue
                if module.endswith(".html"):
                    if os.path.basename(module) in text:
                        hit.append(entry)
                    continue
                tree, _ = _load(entry.partition("::")[0])
                if exercises(tree, node, module):
                    hit.append(entry)
            if not hit:
                missing.append(f"{component}: no registered contract test exercises {module}")
    assert not missing, ("A component's registered tests import it but never call it (or only patch it) — "
                         "register a contract test that runs it:\n" + "\n".join(missing))


def test_the_other_gate_files_keep_their_mark():
    lost = sorted(f for f in ALSO_IN_THE_SLICE if not _in_slice(f)[1])
    assert lost == [], f"these files left the deploy gate's slice (their contract mark is gone): {lost}"


# ── the rule itself, on synthetic test files ──────────────────────────────

def _src(text):
    return ast.parse(text)


def test_an_import_alone_or_a_patched_stub_is_not_coverage():
    unused = _src("import time_utils, metrics  # noqa: F401\n\ndef test_x():\n    assert True\n")
    assert not exercises(unused, unused, "metrics")
    stub = _src("import time_utils\n\ndef test_x(monkeypatch):\n"
                "    monkeypatch.setattr(time_utils, 'restaurant_now', lambda *a: 1)\n"
                "    assert time_utils.restaurant_now() == 1\n")
    assert not exercises(stub, stub, "time_utils")


def test_a_call_an_attribute_or_a_lazy_import_is_coverage():
    called = _src("import time_utils\n\ndef test_x():\n    assert time_utils.restaurant_tz(None)\n")
    assert exercises(called, called, "time_utils")
    lazy = _src("def test_x():\n    from time_utils import mdy\n    assert mdy('2026-10-27')\n")
    assert exercises(lazy, lazy, "time_utils")
    sub = _src("def test_x():\n    import dsr.engine as eng\n    eng.build(1)\n")
    assert exercises(sub, sub, "dsr")
    bp = _src("import mobile_api\n\ndef test_x(app):\n    app.register_blueprint(mobile_api.mobile_bp)\n")
    assert exercises(bp, bp, "mobile_api")


def test_a_registered_test_is_found_by_its_name_and_its_mark_read():
    tree = _src("import pytest\n\n@pytest.mark.contract\ndef test_a():\n    pass\n\ndef test_b():\n    pass\n\n"
                "class TestC:\n    @pytest.mark.contract\n    def test_d(self):\n        pass\n")
    node, chain = _find(tree, "test_a")
    assert node.name == "test_a" and any(_is_contract_mark(d) for d in node.decorator_list)
    node, chain = _find(tree, "test_b")
    assert not any(_is_contract_mark(d) for n in chain for d in n.decorator_list)
    node, chain = _find(tree, "TestC::test_d")
    assert node.name == "test_d" and any(_is_contract_mark(d) for n in chain for d in n.decorator_list)
    assert _find(tree, "test_gone")[0] is None
