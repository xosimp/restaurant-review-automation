"""The schedule modules' layers (ARCHITECTURE_MANIFEST.md §3a; blind re-audit
10/11/26 ARCH-3): 33 scheduling modules imported each other in one cycle,
mostly through imports inside functions, so no module could be read, tested
or replaced alone. The manifest's table gives each one a layer; this file
fails on any import — at module scope or inside a function — that reaches a
higher layer, on any new cycle among the imports, and on a schedule module
the table leaves out. The few two-way dependencies still standing are named
in the manifest and listed here; the list may only shrink."""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "ARCHITECTURE_MANIFEST.md")

# Upward imports still standing (importer, imported): each is named in §3a.
ALLOWED_UPWARD = {
    ("schedule_rules", "schedule_memory"),
}
# Same-layer cycles still standing: each set is one strongly connected group of
# the imports within a layer. A group may lose members; none may gain one.
ALLOWED_CYCLES = (
    {"schedule_rules", "owner_rules", "schedule_note_rules", "schedule_dedicated"},
    {"schedule_versions", "schedule_memory", "schedule_intel", "schedule_learning", "schedule_experiments"},
    {"schedule_engine", "schedule_job", "schedule_scorecard"},
)
# Whole-graph cycles (every layer at once) still standing: the two groups above
# joined by the one upward edge (rules → memory) plus the rules' own review, and
# the entry point's group.
ALLOWED_WHOLE = (
    {"schedule_rules", "owner_rules", "schedule_note_rules", "schedule_dedicated", "schedule_review",
     "schedule_versions", "schedule_memory", "schedule_intel", "schedule_learning", "schedule_experiments"},
    {"schedule_engine", "schedule_job", "schedule_scorecard"},
)


def _is_schedule(name: str) -> bool:
    return name.startswith("schedule_") or name in ("labor", "owner_rules", "shift_quality")


def _layers() -> dict:
    """{module: layer index} from the manifest's §3a table."""
    text = open(MANIFEST, encoding="utf-8").read()
    section = text.split("### 3a.", 1)[1].split("\n## ", 1)[0]
    out = {}
    for line in section.splitlines():
        m = re.match(r"^\| S(\d) [^|]+\| (.+) \|$", line)
        if m:
            for name in re.findall(r"`([\w]+)`", m.group(2)):
                out[name] = int(m.group(1))
    return out


def _modules() -> list:
    return sorted(f[:-3] for f in os.listdir(ROOT) if f.endswith(".py") and _is_schedule(f[:-3]))


def _imports(mod: str) -> dict:
    """{imported schedule module: [(line, at module scope)]} — import and
    from-import statements anywhere in the file, `__import__("x")` and
    `importlib.import_module("x")` with a literal name."""
    tree = ast.parse(open(os.path.join(ROOT, mod + ".py"), encoding="utf-8").read())
    out = {}

    def add(name, node, top):
        name = (name or "").split(".")[0]
        if _is_schedule(name) and name != mod:
            out.setdefault(name, []).append((node.lineno, top))

    def walk(node, top):
        for ch in ast.iter_child_nodes(node):
            inner = top and not isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))
            if isinstance(ch, ast.Import):
                for a in ch.names:
                    add(a.name, ch, top)
            elif isinstance(ch, ast.ImportFrom) and ch.level == 0:
                add(ch.module, ch, top)
            elif isinstance(ch, ast.Call) and ch.args and isinstance(ch.args[0], ast.Constant) \
                    and isinstance(ch.args[0].value, str):
                f = ch.func
                if (isinstance(f, ast.Name) and f.id == "__import__") or \
                        (isinstance(f, ast.Attribute) and f.attr == "import_module"):
                    add(ch.args[0].value, ch, top)
            walk(ch, inner)
    walk(tree, True)
    return out


def _graph() -> dict:
    return {m: _imports(m) for m in _modules()}


def _sccs(nodes, edges) -> list:
    """Tarjan: the strongly connected groups of more than one module."""
    index, low, stack, on, out = {}, {}, [], set(), []
    counter = [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in edges.get(v, ()):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            group = set()
            while True:
                w = stack.pop()
                on.discard(w)
                group.add(w)
                if w == v:
                    break
            if len(group) > 1:
                out.append(group)
    for n in sorted(nodes):
        if n not in index:
            visit(n)
    return out


def test_every_schedule_module_has_a_layer():
    layers = _layers()
    missing = [m for m in _modules() if m not in layers]
    gone = [m for m in layers if m not in _modules()]
    assert not missing, f"give these a layer in ARCHITECTURE_MANIFEST.md §3a: {missing}"
    assert not gone, f"§3a lists modules that no longer exist: {gone}"


def test_no_schedule_module_imports_a_higher_layer():
    """At module scope or inside a function: an import reaches its own layer
    or a lower one, except the upward edges §3a names."""
    layers = _layers()
    bad = []
    for mod, deps in _graph().items():
        for dep, sites in deps.items():
            if layers.get(dep, -1) > layers.get(mod, -1) and (mod, dep) not in ALLOWED_UPWARD:
                bad.append(f"{mod} (S{layers.get(mod)}) → {dep} (S{layers.get(dep)}) at line {sites[0][0]}")
    assert not bad, "an import reaches a higher schedule layer:\n" + "\n".join(sorted(bad))


def test_module_scope_imports_go_strictly_down_or_across():
    """No exception at module scope: an upward import there is an import-time cycle waiting to happen."""
    layers = _layers()
    bad = [f"{mod} → {dep}" for mod, deps in _graph().items() for dep, sites in deps.items()
           if any(top for _ln, top in sites) and layers.get(dep, -1) > layers.get(mod, -1)]
    assert not bad, bad


def test_the_upward_exceptions_still_exist():
    """A fixed exception comes off ALLOWED_UPWARD (and out of §3a)."""
    g = _graph()
    stale = [e for e in ALLOWED_UPWARD if e[1] not in g.get(e[0], {})]
    assert not stale, f"these no longer import upward — remove them from the allowed list and §3a: {stale}"


def test_no_new_cycle_inside_a_layer():
    layers = _layers()
    g = _graph()
    same = {m: {d for d in deps if layers.get(d) == layers.get(m)} for m, deps in g.items()}
    for group in _sccs(g, same):
        assert any(group <= ok for ok in ALLOWED_CYCLES), \
            f"a new cycle among the schedule modules of one layer: {sorted(group)}"


def test_no_new_cycle_across_the_schedule_modules():
    g = _graph()
    whole = {m: set(deps) for m, deps in g.items()}
    groups = _sccs(g, whole)
    for group in groups:
        assert any(group <= ok for ok in ALLOWED_WHOLE), \
            f"a new import cycle among the schedule modules: {sorted(group)}"
    # The ratchet's measure: it was one group of 33 (ARCH-3).
    assert max((len(x) for x in groups), default=0) <= 10


@pytest.mark.parametrize("pure", ["schedule_time", "schedule_roles", "schedule_learned", "schedule_rows",
                                  "shift_quality", "schedule_objective", "schedule_requirements"])
def test_the_pure_layers_import_nothing_above_them(pure):
    """The scorer, the objective and the vocabulary they share import no rule,
    memory, engine or labor module (the auditor's "pure" shift_quality
    imported schedule_rules and schedule_memory)."""
    layers = _layers()
    above = [d for d in _imports(pure) if layers.get(d, 9) > layers[pure]]
    assert not above, (pure, above)
