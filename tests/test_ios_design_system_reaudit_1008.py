"""iOS blind re-audit, design-system fix round (10/8/26, S1–S17).

Source-level checks for the shared components every screen builds on, so a
later edit can't quietly undo a rule: the fill that carries white words
darkens under Increase Contrast, chips pick a readable ink, the custom
controls speak to VoiceOver, notes say how serious they are, and the
widgets keep an 11/12pt floor.
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI")
APP = os.path.join(IOS, "CavnarAI")
DS = os.path.join(APP, "DesignSystem")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def _hex(colorset, appearance):
    data = json.loads(_read(APP, "Assets.xcassets", "Colors", colorset + ".colorset", "Contents.json"))
    for c in data["colors"]:
        key = tuple(sorted(a["value"] for a in c.get("appearances", [])))
        if key == appearance:
            comp = c["color"]["components"]
            return tuple(round(float(comp[k]) * 255) for k in ("red", "green", "blue"))
    raise AssertionError(f"{colorset} has no {appearance} variant")


def _lum(rgb):
    def lin(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _white_contrast(rgb):
    return 1.05 / (_lum(rgb) + 0.05)


def test_s1_ember_fill_darkens_under_increase_contrast():
    normal = _hex("EmberFill", ("dark",))
    high = _hex("EmberFill", ("dark", "high"))
    assert normal == _hex("Ember", ("dark",)), "EmberFill keeps the brand look"
    assert _white_contrast(high) >= 4.5 > _white_contrast(_hex("Ember", ("dark", "high")))
    vm = _read(DS, "ViewModifiers.swift")
    surface = vm[vm.index("struct CavnarPremiumButtonSurface"):vm.index("struct CavnarPrimaryButtonStyle")]
    assert "shape.fill(Color.cavnarEmberFill" in surface
    assert 'static let cavnarEmberFill = Color("EmberFill")' in _read(DS, "Color+Cavnar.swift")


def test_s2_chip_ink_follows_the_tone():
    vm = _read(DS, "ViewModifiers.swift")
    chip = vm[vm.index("struct CavnarChipButtonStyle"):vm.index("struct CavnarGlassButtonStyle")]
    assert "tone.resolve(in: environment)" in chip
    assert "contrast(1.0, tone) >= 4.5" in chip
    assert "case .tinted(let toneText)" in chip
    # White is no longer forced on every tone.
    assert re.search(r"\.foregroundStyle\(\.white\)", chip) is None


def test_s3_segmented_control_is_a_picker_to_voiceover():
    seg = _read(DS, "CavnarSegmentedControl.swift")
    assert ".accessibilityRepresentation {" in seg and ".pickerStyle(.segmented)" in seg
    assert ".padding(.vertical, 5)" in seg  # 34pt drawn, 44pt row


def test_s4_reading_paragraphs_lift_past_the_cap():
    kit = _read(DS, "CavnarAnswerKit.swift")
    assert "role == .lead || role == .body" in kit
    assert "cavnarReadingSize(upTo: .accessibility2)" in kit


def test_s5_widgets_keep_the_floor():
    small = re.compile(r"\.cavnar(?:Body|Number)\(\s*(\d+(?:\.\d+)?)")
    for folder in ("CavnarWidgets",):
        for name in os.listdir(os.path.join(IOS, folder)):
            if not name.endswith(".swift"):
                continue
            src = _read(IOS, folder, name)
            for m in small.finditer(src):
                size = float(m.group(1))
                assert size >= 12 or (size >= 11 and "weight: 700" in src[m.end():m.end() + 16]), (name, size)


def test_s6_pinned_bar_note_has_a_tone():
    kit = _read(DS, "CavnarAnswerKit.swift")
    assert "enum CavnarNoteTone" in kit and "case hint, warning, error" in kit
    assert "var noteTone: CavnarNoteTone = .hint" in kit
    assert ".cavnarRedText : .cavnarAmber" in kit


def test_s7_hide_label_follows_the_show_label():
    kit = _read(DS, "CavnarAnswerKit.swift")
    card = kit[kit.index("struct CavnarAnswerCard"):kit.index("// MARK: - Evidence disclosure")]
    assert 'Text(showingDetail ? "Hide the evidence"' not in card
    assert "CavnarEvidenceDisclosure(label: detailLabel" in card
    assert "static func hideLabel(for label: String) -> String" in kit
    assert "Self.hideLabel(for: label)" in kit
    assert "Could also be: " in card and ".lineLimit(2)" not in card


def test_s8_consultant_sheet_uses_the_answer_anatomy():
    ai = _read(DS, "AIConsultantView.swift")
    assert ".lineLimit(1)" not in ai
    assert 'CavnarEvidenceSection(label: "See the evidence")' in ai
    assert "insight.recConfidence(at: index)" in ai
    model = _read(APP, "Models", "AIInsight.swift")
    assert 'case recConfidence = "insight_rec_confidence"' in model


def test_s9_the_decline_still_reads_pass():
    # The re-audit asked for "Not for us"; the owner's rule (10/1/26) is
    # "Pass" everywhere, web and iOS. Kept.
    assert 'case .notForUs:  return "Pass"' in _read(DS, "RecAnswerRow.swift")
    assert 'title: "Pass"' in _read(APP, "Push", "PushManager.swift")


def test_s10_kicker_header_and_web_row_subtitle():
    kit = _read(DS, "CavnarAnswerKit.swift")
    assert "var isHeader: Bool = true" in kit
    assert ".accessibilityAddTraits(isHeader ? .isHeader : [])" in kit
    assert "accessibilityText(title: title, subtitle: subtitle, actionLabel: actionLabel)" in kit


def test_s11_no_status_codes_in_owner_copy():
    api = _read(APP, "Core", "APIClient.swift")
    assert not re.search(r'\?\? "Something went wrong \(', api)
    assert '?? "Your session expired' not in api and 'message: "Session expired"' not in api
    assert "?? Self.genericFailureMessage" in api
    assert 'static let sessionEndedMessage = "Your session ended \\u{2014} sign in again."' in api


def test_s12_s14_glyph_buttons_have_names():
    vm = _read(DS, "ViewModifiers.swift")
    assert vm.count('.accessibilityLabel("Back")') == 2
    kb = _read(DS, "KeyboardNavToolbar.swift")
    for word in ('"Previous"', '"Next"', '"Done"'):
        assert word in kb
    assert ".frame(width: 44, height: 44)" in kb
    assert '.accessibilityLabel("More options")' in _read(DS, "CavnarSplitButton.swift")
    motion = _read(DS, "CavnarMotion.swift")
    badge = motion[motion.index("struct CavnarAlertBadge"):]
    assert "cavnarNumber(CavnarType.tag" in badge and "cavnarRedFill" in badge
    assert _white_contrast(_hex("RedFill", ("dark",))) >= 4.5


def test_s13_literal_helpers_scale_like_the_roles():
    font = _read(DS, "Font+Cavnar.swift")
    body = font[font.index("static func bodyTextStyle"):font.index("private static func headlineTextStyle")]
    assert "case ..<17:  return .callout" in body and "case ..<16:  return .subheadline" in body


def test_s15_soft_button_words_are_ember2():
    vm = _read(DS, "ViewModifiers.swift")
    soft = vm[vm.index("struct CavnarSoftButtonStyle"):vm.index("struct CavnarChipButtonStyle")]
    assert ".foregroundStyle(Color.cavnarEmber2)" in soft


def test_s16_one_staff_portal_row():
    ds = _read(ROOT, "DESIGN_SYSTEM.md")
    assert ds.count("| Staff portal |") == 1
    assert "**iPhone app only** since 9/30/26" not in ds
