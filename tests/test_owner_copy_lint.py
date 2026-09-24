"""scripts/check_owner_copy.py — the ratchet over hand-written owner copy
(NS1 V7 / NS3 R9 banned labels). Holds at its BASELINE; fails on a new one."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_owner_copy as lint  # noqa: E402


def test_owner_copy_holds_at_its_baseline():
    out = subprocess.run([sys.executable, "scripts/check_owner_copy.py"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_python_reads_string_literals_not_comments_or_docstrings():
    src = ('"""Value delivered is banned — a docstring may say so."""\n'
           "# a comment about value delivered\n"
           "def f():\n"
           '    """Also a docstring: typical restaurant."""\n'
           '    label = "Monthly savings"\n'
           '    ok = "Measured results"\n'
           '    kept = "Value delivered"  # owner-copy-ok: a prompt telling the model not to write it\n'
           '    return f"{label} — optimized"\n')
    hits = lint.scan_source(src, "py")
    assert sorted(h[1] for h in hits) == ["optimized / optimal", "savings label on a gap"]


def test_swift_reads_string_literals_not_comments():
    src = ('// "Value delivered" in a comment\n'
           'Text("Annual savings")\n'
           'Text("Gap to target") // was "Monthly savings"\n')
    assert [h[1] for h in lint.scan_source(src, "swift")] == ["savings label on a gap"]


def test_html_skips_comments():
    src = ('<!-- Value delivered, retired -->\n'
           '<div>{# the one number #}High confidence</div>\n'
           "// Generate optimized schedule\n"
           "<button class=\"cbtn\">Generate optimized schedule</button>\n")
    assert sorted(h[1] for h in lint.scan_source(src, "html")) == ["band word as confidence", "optimized / optimal"]


def test_the_ratchet_fails_when_the_count_rises(monkeypatch):
    real = lint.scan()
    monkeypatch.setattr(lint, "scan", lambda: real + [("templates/x.html", 1, "claw back", "claw back")])
    assert lint.main([]) == 1
    monkeypatch.setattr(lint, "scan", lambda: real)
    assert lint.main([]) == 0


def test_every_file_family_in_scope_is_read():
    kinds = {k for _rel, k in lint._files()}
    assert kinds == {"py", "html", "swift"}
    rels = {rel for rel, _k in lint._files()}
    for name in lint.PY_FILES:
        assert name in rels
    assert any(r.startswith("public/") for r in rels) and any(r.startswith("templates/") for r in rels)
    assert not any("Tests/" in r for r in rels)
