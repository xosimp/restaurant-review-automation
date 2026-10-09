"""iOS text inks ratchet (re-audit S17, 10/8/26).

Two inks are for fills, bars, dots and icons, not for words:

  - `.cavnarRed` (#E3333F) is 4.5:1 on Paper and ~4.3:1 on a card, so small
    red TEXT takes `.cavnarRedText` (#F05A63, 5.9:1) — DESIGN_SYSTEM.md §2.
  - `.cavnarEmber` (#D4583A) is 4.9:1 on Paper and under 4:1 on an ember
    wash; ember words take `.cavnarEmber2` (#E8956A, 8.3:1). Ember as text
    survives only where it is a brand moment (the owner's name in the AI
    consultant's headline).

This counts the text sites still using each — a `Text(...)` /
`CavnarMixedText` / `HomeMixedText` / `Label(...)` whose modifier chain sets
`.foregroundStyle(.cavnarRed)` (or Ember, or `foregroundColor`), and any
`.cavnarText(_, color: .cavnarRed)` — across the app target. The counts may
only fall: when a sweep lowers one, lower its BASELINE in the same commit.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")

# Counts after the post-merge sweep (10/8/26, W2/W3): red text is gone;
# ember text is the owner's name in the AI consultant's headline and the
# Guest text club's icon-only Copy label. They may only go down.
BASELINE = {"cavnarRed": 0, "cavnarEmber": 2}

_TEXT_BASE = re.compile(r"\b(?:Text|CavnarMixedText|Label|TextField|SecureField)\(|HomeMixedText\.make\(")
_NOT_TEXT_BASE = re.compile(r"\b(?:Image|Circle|Capsule|Rectangle|RoundedRectangle|Path|Gauge|ProgressView|Toggle)\b")


def _fg(token):
    # .foregroundStyle(.cavnarRed) / (Color.cavnarRed) / foregroundColor(...),
    # the exact token — not cavnarRedText / cavnarRedBg / cavnarRedFill.
    return re.compile(r"\.foreground(?:Style|Color)\(\s*(?:Color)?\.%s\s*\)" % token)


def _role(token):
    return re.compile(r"\.cavnarText\([^)\n]*color:\s*(?:Color)?\.%s\s*\)" % token)


def _swift_files(root):
    for dirpath, _, names in os.walk(root):
        for n in sorted(names):
            if n.endswith(".swift"):
                yield os.path.join(dirpath, n)


def text_ink_sites(src, token):
    """Count the text sites in `src` inked with `token`."""
    lines = src.split("\n")
    fg, role = _fg(token), _role(token)
    n = 0
    for i, line in enumerate(lines):
        if role.search(line):
            n += len(role.findall(line))
            continue
        if not fg.search(line):
            continue
        # Walk back up the modifier chain to the view the modifier styles.
        j = i
        base = line
        while j >= 0:
            stripped = lines[j].strip()
            if j < i and not stripped.startswith("."):
                base = lines[j]
                break
            if j == i and not stripped.startswith("."):
                base = lines[j]
                break
            j -= 1
        if _TEXT_BASE.search(base) and not _NOT_TEXT_BASE.search(base.split("(")[0]):
            n += 1
    return n


def _counts():
    totals = {}
    for token in BASELINE:
        per_file = {}
        for path in _swift_files(APP):
            with open(path, encoding="utf-8") as f:
                c = text_ink_sites(f.read(), token)
            if c:
                per_file[os.path.relpath(path, APP)] = c
        totals[token] = (sum(per_file.values()), per_file)
    return totals


def test_the_counter_reads_what_it_should():
    src = """
    Text("Over target")
        .font(.cavnar(.caption))
        .foregroundStyle(Color.cavnarRed)
    Image(systemName: "xmark")
        .foregroundStyle(Color.cavnarRed)
    Circle().fill(Color.cavnarRed)
    Text("ok").foregroundStyle(.cavnarRedText)
    Text("bad").cavnarText(.caption, color: .cavnarRed)
    CavnarMixedText(line, role: .secondary)
        .foregroundColor(.cavnarRed)
    """
    assert text_ink_sites(src, "cavnarRed") == 3
    assert text_ink_sites('Text("a").foregroundStyle(Color.cavnarEmber)\n', "cavnarEmber") == 1
    assert text_ink_sites('Text("a").foregroundStyle(Color.cavnarEmber2)\n', "cavnarEmber") == 0


def test_red_and_ember_text_only_fall():
    totals = _counts()
    for token, baseline in BASELINE.items():
        total, per_file = totals[token]
        worst = sorted(per_file.items(), key=lambda kv: -kv[1])[:8]
        assert total <= baseline, (
            f"{total} text sites inked .{token} (baseline {baseline}). Small red text is "
            f".cavnarRedText; ember words are .cavnarEmber2. Most: {worst}"
        )
