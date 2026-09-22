"""ARCHITECTURE_MANIFEST.md is the map; these tests keep it true.

Three things are checkable: every root module appears in the module table,
every module's module-scope imports stay within the layer the table gives
it (lazy imports inside functions are the sanctioned way upward), and
every tracked top-level folder is accounted for in the folder table.
"""
import ast
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "ARCHITECTURE_MANIFEST.md")

# Module-scope upward edges the manifest names as known exceptions (§3).
ALLOWED_UPWARD = {
    ("ops", "emails"),
    ("mobile_api", "client_api"),
}


def _manifest():
    return open(MANIFEST, encoding="utf-8").read()


def _module_layers():
    """{module: layer} from the §4 table. A row may name two modules
    (`a` / `b` | 2 / 3) and `intelligence.*` covers the package."""
    section = _manifest().split("## 4. Module table", 1)[1].split("\n## ", 1)[0]
    layers = {}
    for line in section.splitlines():
        m = re.match(r"^\| (.+?) \| (.+?) \| .*\|$", line)
        if not m or m.group(1).startswith("Module") or set(m.group(1)) <= set("-"):
            continue
        names = re.findall(r"`([\w.*]+)`", m.group(1))
        lays = re.findall(r"\d", m.group(2).split("(")[0]) or re.findall(r"\d", m.group(2))
        if not names:
            continue
        if len(lays) == 1:
            lays = lays * len(names)
        for n, l in zip(names, lays):
            layers[n] = int(l)
    # intelligence package: stated inline in its row
    layers.setdefault("intelligence.*", 2)
    for leaf in ("stats", "privacy", "categories"):
        layers[f"intelligence.{leaf}"] = 0
    return layers


def _tracked_modules():
    out = subprocess.check_output(["git", "ls-files", "*.py"], cwd=ROOT, text=True).split()
    return [f for f in out if not f.startswith(("tests/", "scripts/", "brand/"))]


def _layer_of(mod, layers):
    if mod in layers:
        return layers[mod]
    if mod.startswith("intelligence"):
        return layers["intelligence.*"]
    return None


def _module_scope_imports(path, mod):
    """Local modules imported at column 0 (module scope), resolved to names."""
    tree = ast.parse(open(os.path.join(ROOT, path), encoding="utf-8").read())
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)) or node.col_offset != 0:
            continue
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name)
        else:
            if node.level:  # relative: inside intelligence/
                base = "intelligence"
                found.add(base + ("." + node.module if node.module else ""))
                if node.module is None:
                    for a in node.names:
                        found.add("intelligence." + a.name)
            elif node.module:
                found.add(node.module)
    return found


def test_every_root_module_is_in_the_module_table():
    layers = _module_layers()
    missing = []
    for path in _tracked_modules():
        mod = path[:-3].replace("/", ".")
        if mod == "intelligence.__init__":
            continue
        if _layer_of(mod, layers) is None:
            missing.append(mod)
    assert missing == [], f"add these modules to ARCHITECTURE_MANIFEST.md §4: {missing}"


def test_module_scope_imports_respect_the_layers():
    layers = _module_layers()
    local = {p[:-3].replace("/", ".") for p in _tracked_modules()} | {"intelligence"}
    violations = []
    for path in _tracked_modules():
        mod = path[:-3].replace("/", ".")
        if mod == "intelligence.__init__":
            mod = "intelligence"
        mine = _layer_of(mod, layers)
        if mine is None:
            continue
        for imp in _module_scope_imports(path, mod):
            target = imp if imp in local else imp.split(".")[0]
            if target not in local or target == mod:
                continue
            theirs = _layer_of(target, layers)
            if theirs is None:
                theirs = layers["intelligence.*"] if target.startswith("intelligence") else None
            if theirs is not None and theirs > mine and (mod, target) not in ALLOWED_UPWARD:
                violations.append(f"{mod} (L{mine}) imports {target} (L{theirs}) at module scope")
    assert violations == [], "\n".join(violations) + "\n— import it inside the function, move the module, or list the exception in ARCHITECTURE_MANIFEST.md §3 and ALLOWED_UPWARD"


def test_every_top_level_folder_is_in_the_folder_table():
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).split()
    folders = sorted({p.split("/")[0] for p in tracked if "/" in p})
    section = _manifest().split("## 1. Top-level folders", 1)[1].split("\n## ", 1)[0]
    missing = [f for f in folders if f"`{f}/`" not in section and f"`{f}" not in section]
    assert missing == [], f"add these folders to ARCHITECTURE_MANIFEST.md §1: {missing}"


def test_the_manifest_is_indexed_and_names_its_own_test():
    assert "ARCHITECTURE_MANIFEST.md" in open(os.path.join(ROOT, "CLAUDE.md"), encoding="utf-8").read()
    assert "tests/test_architecture_manifest.py" in _manifest()
