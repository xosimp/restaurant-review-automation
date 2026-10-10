"""Structured outputs reject array- and string-length constraints
("For 'array' type, property 'maxItems' is not supported"): a schema that
carries one fails every call with a 400. It broke the nightly report on
9/23/26 and the reply reviewer on 10/9/26. Lengths are held by the prompt
and by code after the reply, never by the wire schema."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED = re.compile(r"""["'](maxItems|minItems|maxLength|minLength|uniqueItems)["']\s*:""")


def test_no_schema_carries_a_length_constraint():
    hits = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if rel.parts[0] in ("tests", ".venv", "venv", "node_modules", ".claude") or "site-packages" in rel.parts:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if BANNED.search(line) and not line.lstrip().startswith("#"):
                hits.append(f"{rel}:{i}: {line.strip()[:120]}")
    assert not hits, "\n".join(hits)
