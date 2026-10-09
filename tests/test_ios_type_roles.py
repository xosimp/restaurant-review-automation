"""iOS readability round (10/8/26): text is set by ROLE, not by point size.

Phase 1 added the roles (`CavnarText`, `.cavnarText(_:)`, `Font.cavnar(_:)`),
the spacing scale, the hit-target modifier and the answer kit; the module
screens move onto them one by one. This is the ratchet that keeps the move
one-way: the count of literal-size type calls under Features/ may only fall.
When a module round lowers it, lower BASELINE to the new count in the same
commit.

Counted (under ios/CavnarAI/CavnarAI/Features):
  - `cavnarBody(` / `cavnarHeadline(` / `cavnarNumber(` whose size argument
    is a numeric literal — `.cavnarBody(15, weight: 600)`;
  - `HomeMixedText.make(…, size: <literal>, …)`.
A `CavnarType.*` token or a role is not counted.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")
FEATURES = os.path.join(APP, "Features")

# Today's count after phase 1 (10/8/26). It may only go down.
BASELINE = 86

_LITERAL_HELPER = re.compile(r"\bcavnar(?:Body|Headline|Number)\(\s*-?\d+(?:\.\d+)?\s*[,)]")
_MAKE = "HomeMixedText.make("
_SIZE_LITERAL = re.compile(r"\bsize:\s*-?\d+(?:\.\d+)?\b")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def _swift_files(root):
    for dirpath, _, names in os.walk(root):
        for n in sorted(names):
            if n.endswith(".swift"):
                yield os.path.join(dirpath, n)


def _call_args(src, start):
    """The argument text of the call whose "(" sits just before `start`."""
    depth, i = 1, start
    while i < len(src) and depth:
        c = src[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return src[start:i - 1]


def literal_type_calls(src):
    n = len(_LITERAL_HELPER.findall(src))
    i = src.find(_MAKE)
    while i != -1:
        args = _call_args(src, i + len(_MAKE))
        if _SIZE_LITERAL.search(args):
            n += 1
        i = src.find(_MAKE, i + len(_MAKE))
    return n


def _count():
    per_file = {}
    for path in _swift_files(FEATURES):
        n = literal_type_calls(_read(path))
        if n:
            per_file[os.path.relpath(path, APP)] = n
    return sum(per_file.values()), per_file


def test_the_counter_reads_what_it_should():
    src = """
    Text("a").font(.cavnarBody(15, weight: 600))
    Text("b").font(.cavnarHeadline(22))
    Text("c").font(.cavnarNumber(40.5, weight: 700))
    Text("d").font(.cavnarBody(CavnarType.body))
    Text("e").font(.cavnar(.body))
    HomeMixedText.make(s, size: 13.5, weight: f(500), color: .cavnarInk3)
    HomeMixedText.make(s, size: CavnarType.caption, weight: 500)
    HomeMixedText.make(s, role: .body)
    """
    assert literal_type_calls(src) == 4


def test_literal_type_sizes_under_features_only_fall():
    total, per_file = _count()
    worst = sorted(per_file.items(), key=lambda kv: -kv[1])[:10]
    assert total <= BASELINE, (
        f"{total} literal-size type calls under Features/ (baseline {BASELINE}). "
        f"Use a role (.cavnarText(.body), Font.cavnar(.caption), HomeMixedText.make(_:role:)) "
        f"or a CavnarType token. Most: {worst}"
    )


def test_the_roles_exist_with_their_sizes():
    font = _read(APP, "DesignSystem", "Font+Cavnar.swift")
    assert "enum CavnarText: CaseIterable" in font
    roles = re.search(r"enum CavnarText[^{]*\{\s*case ([^\n]+)\n\s*case ([^\n]+)\n", font)
    names = {r.strip() for r in (roles.group(1) + "," + roles.group(2)).split(",")}
    assert names == {"display", "title", "headline", "lead", "body", "label", "secondary", "caption",
                     "kicker", "tag", "figureXL", "figureL", "figureM", "figureS"}
    assert "static func cavnar(_ role: CavnarText) -> Font" in font
    assert "func cavnarText(_ role: CavnarText, color: Color? = nil) -> some View" in font
    # The floor: caption 13, kicker 12, tag 11 — the token set matches.
    for name, size in (("caption", "13"), ("kicker", "12"), ("tag", "11"), ("secondary", "14"),
                       ("body", "16"), ("emphasis", "18")):
        assert re.search(r"static let %s: CGFloat = %s\n" % (name, size), font), name
    # Figures follow Dynamic Type like words, with tabular digits.
    assert "UIFontMetrics(forTextStyle: uiTextStyle(for: size))" not in font
    assert ".custom(spaceGroteskStatic(weight), size: size, relativeTo: style).monospacedDigit()" in font


def test_the_static_figure_faces_ship_in_every_target():
    yml = _read(ROOT, "ios", "CavnarAI", "project.yml")
    for w in ("Regular", "Medium", "SemiBold", "Bold"):
        assert yml.count(f"- SpaceGroteskStatic-{w}.ttf") == 4, w
        assert os.path.exists(os.path.join(APP, "Fonts", f"SpaceGroteskStatic-{w}.ttf")), w


def test_the_kit_the_module_rounds_build_on():
    vm = _read(APP, "DesignSystem", "ViewModifiers.swift")
    assert "enum CavnarSpace" in vm and "static let cardPadding: CGFloat = 20" in vm
    assert "func cavnarHitTarget() -> some View" in vm
    assert "frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())" in vm
    assert "Color.cavnarEmber.opacity(0.22), Color.cavnarEmber.opacity(0)" in vm
    kit = _read(APP, "DesignSystem", "CavnarAnswerKit.swift")
    for name in ("struct CavnarKicker", "struct CavnarAnswerCard", "struct CavnarWebLinkRow",
                 "struct CavnarPinnedBar", "struct CavnarMoreDisclosure", "struct CavnarMixedText"):
        assert name in kit, name
    # The web link never routes back into the app through a universal link.
    assert "SFSafariViewController" in kit and "openURL" not in kit.split("struct CavnarWebLinkRow", 1)[1].split("struct CavnarSafariView", 1)[0].replace("`openURL`", "")
    assert 'var detailLabel: String = "See the evidence"' in kit


def test_ink_tiers_and_text_safe_red():
    colors = os.path.join(APP, "Assets.xcassets", "Colors")

    def variants(name):
        import json
        out = {}
        for c in json.loads(_read(colors, name + ".colorset", "Contents.json"))["colors"]:
            key = tuple(sorted(a["value"] for a in c.get("appearances", [])))
            comp = c["color"]["components"]
            out[key] = "#%02X%02X%02X" % tuple(round(float(comp[k]) * 255) for k in ("red", "green", "blue"))
        return out

    assert variants("Ink2")[("dark",)] == "#BDB5A8"
    assert variants("Ink3")[("dark",)] == "#948C80"
    assert variants("RedText")[("dark",)] == "#F05A63"
    for name in ("Ink2", "Ink3", "Ember", "Red", "RedText"):
        assert ("dark", "high") in variants(name), name
    swift = _read(APP, "DesignSystem", "Color+Cavnar.swift")
    assert 'static let cavnarRedText = Color("RedText")' in swift
