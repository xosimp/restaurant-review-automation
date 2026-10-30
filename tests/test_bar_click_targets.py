"""Every data-br-* action button the Bar draws is one its click handler
listens for (owner, 10/30/26: "+ Add a bottle bought elsewhere" did nothing —
its attribute was missing from the handler's closest() list)."""
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "templates" / "dashboard.html").read_text()


def test_every_bar_branch_attribute_is_a_click_target():
    m = re.search(r"el\.addEventListener\('click', function \(e\) \{\s*var t = e\.target && e\.target\.closest \? "
                  r"e\.target\.closest\('([^']+)'\)", HTML)
    assert m, "the Bar's click handler moved"
    listened = set(re.findall(r"\[(data-br-[a-z-]+)\]", m.group(1)))
    start = m.end()
    end = HTML.index("el.addEventListener(", start)
    branches = set(re.findall(r"a\('(data-br-[a-z-]+)'\) != null", HTML[start:end]))
    assert branches - listened == set()
