import SwiftUI

// The Send sheet's decision, drawn from the one pre-publish payload
// (PrePublishReview.swift): the verdict first, then what must be fixed (red
// for the law and the owner's non-negotiables — TRUST-27), what to decide,
// the numbers with how far to trust them, and — one tap away each — what
// changed, where the week is fragile, why these people, and how sure.
// "Web explains. iPhone decides.": big words, few of them, every line with
// its action.

// MARK: - Verdict

/// "Ready to send" / "Ready after 2 decisions" / "Not ready: …" — the
/// server's sentence, in its tone.
struct PublishVerdictCard: View {
    let verdict: PublishReview.Verdict

    private var tone: Color {
        switch verdict.tone {
        case "green": return .cavnarGreen
        case "red": return .cavnarRed
        default: return .cavnarAmber
        }
    }

    private var symbol: String {
        switch verdict.tone {
        case "green": return "checkmark.circle.fill"
        case "red": return "xmark.octagon.fill"
        default: return "exclamationmark.circle.fill"
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: CavnarSpace.s) {
            Image(systemName: symbol)
                .font(.cavnar(.headline))
                .foregroundStyle(tone)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                CavnarMixedText(verdict.sentence, role: .lead, color: .cavnarInk)
                if let sub = verdict.subline {
                    CavnarMixedText(sub, role: .secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(CavnarSpace.m)
        .background(RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous).fill(tone.opacity(0.10)))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
            .strokeBorder(tone.opacity(0.35), lineWidth: 1))
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Must fix

/// Each item fixed, or sent past on its own with a reason. A hard item (the
/// law, a manager on the floor, a minor's hours, approved time off) is red;
/// only the softer ones (the budget, the notice window) are amber.
struct PublishMustFixCard: View {
    let items: [PublishReview.MustFix]
    @Binding var overrides: [String: String]
    let busy: String?
    let notes: [String: String]
    let onRun: (PublishReview.Action) -> Void

    @State private var editing: String?
    @State private var draft = ""
    @FocusState private var reasonFocused: Bool

    private var anyHard: Bool { items.contains(where: \.hard) }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker(anyHard ? "Must fix before sending" : "Before this goes out",
                         icon: anyHard ? "xmark.octagon.fill" : "hand.raised.fill",
                         tint: anyHard ? .cavnarRedText : .cavnarAmber)
            ForEach(items) { item in
                row(item)
                if item.id != items.last?.id {
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(CavnarSpace.m)
        .background(RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
            .fill((anyHard ? Color.cavnarRed : Color.cavnarAmber).opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
            .strokeBorder((anyHard ? Color.cavnarRed : Color.cavnarAmber).opacity(0.35), lineWidth: 1))
    }

    @ViewBuilder
    private func row(_ item: PublishReview.MustFix) -> some View {
        let reason = overrides[item.key]
        let tone: Color = item.hard ? .cavnarRed : .cavnarAmber
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(alignment: .top, spacing: CavnarSpace.xs) {
                Circle().fill(tone).frame(width: 8, height: 8).padding(.top, 8)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarMixedText(item.text, role: .body, color: .cavnarInk)
                    if let before = item.overriddenBefore {
                        CavnarMixedText(before, role: .caption)
                    }
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel((item.hard ? "Must fix: " : "Check: ") + item.text)
            if let reason {
                // Sent past on purpose: said, with the words, and undoable.
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    CavnarMixedText(reason.isEmpty ? "You\u{2019}ll send past this."
                                                   : "You\u{2019}ll send past this: \u{201C}\(reason)\u{201D}",
                                    role: .secondary, color: .cavnarInk2)
                    Spacer(minLength: 0)
                    Button {
                        Haptic.light()
                        overrides[item.key] = nil
                    } label: {
                        Text("Undo").cavnarText(.label, color: .cavnarEmber2).cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
            } else if editing == item.key {
                reasonField(item)
            } else {
                actions(item)
            }
        }
        .padding(.vertical, CavnarSpace.xxs)
    }

    @ViewBuilder
    private func actions(_ item: PublishReview.MustFix) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            ForEach(item.fixes) { fix in
                Button {
                    onRun(fix)
                } label: {
                    Group {
                        if busy == fix.id {
                            CavnarShimmerText(text: "Working\u{2026}")
                        } else {
                            Text(fix.buttonLabel)
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy != nil))
                .disabled(busy != nil)
                if let note = notes[fix.id] {
                    CavnarMixedText(note, role: .secondary, color: .cavnarRedText)
                }
            }
            if item.overrideAllowed {
                Button {
                    Haptic.light()
                    draft = ""
                    editing = item.key
                    reasonFocused = true
                } label: {
                    Text("Send anyway: I\u{2019}ll handle it").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            } else if item.fixes.isEmpty {
                CavnarMixedText("This one can\u{2019}t be sent past. Change the week to fix it.",
                                role: .secondary, color: .cavnarInk2)
            }
        }
    }

    private func reasonField(_ item: PublishReview.MustFix) -> some View {
        let words = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        let ready = !item.needsReason || !words.isEmpty
        return VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            Text(item.reasonPrompt).cavnarText(.label)
            TextField(item.needsReason ? "Your reason" : "Your reason (optional)", text: $draft, axis: .vertical)
                .font(.cavnar(.body))
                .foregroundStyle(Color.cavnarInk)
                .lineLimit(1...4)
                .focused($reasonFocused)
                .padding(CavnarSpace.s)
                .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                    .fill(Color.cavnarPaper2))
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                    .strokeBorder(reasonFocused ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
            HStack(spacing: CavnarSpace.xs) {
                Button {
                    Haptic.medium()
                    overrides[item.key] = String(words.prefix(300))
                    editing = nil
                    reasonFocused = false
                } label: {
                    Text("Send past this").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !ready))
                .disabled(!ready)
                Button {
                    editing = nil
                    reasonFocused = false
                } label: {
                    Text("Cancel").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            }
            Text("Kept with the week and shown in History.").cavnarText(.caption)
        }
    }
}

// MARK: - Decide

/// One tap each; the scope the answer holds for is on the button.
struct PublishDecideCard: View {
    let decisions: [PublishReview.Decision]
    let busy: String?
    let notes: [String: String]
    let answered: [String: String]
    let onRun: (PublishReview.Action, String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker(decisions.count == 1 ? "1 decision" : "\(decisions.count) decisions",
                         icon: "arrow.triangle.branch", tint: .cavnarAmber)
            ForEach(decisions) { d in
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    CavnarMixedText(d.text, role: .body, color: .cavnarInk)
                    if let answer = answered[d.key] {
                        HStack(spacing: CavnarSpace.xxs) {
                            Image(systemName: "checkmark").foregroundStyle(Color.cavnarGreen)
                                .accessibilityHidden(true)
                            CavnarMixedText(answer, role: .secondary)
                        }
                    } else {
                        ForEach(d.options) { option in
                            Button {
                                onRun(option, d.key)
                            } label: {
                                Group {
                                    if busy == option.id {
                                        CavnarShimmerText(text: "Saving\u{2026}")
                                    } else {
                                        Text(option.buttonLabel)
                                    }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                            .disabled(busy != nil)
                            if let note = notes[option.id] {
                                CavnarMixedText(note, role: .secondary, color: .cavnarRedText)
                            }
                        }
                    }
                }
                if d.id != decisions.last?.id {
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}

// MARK: - Numbers

/// Labor %, cost, overtime and forecast sales, each with how far to trust
/// it; a saving is a projection and says so — never summed into anything.
struct PublishNumbersCard: View {
    let numbers: PublishReview.Numbers

    static func money(_ v: Double) -> String { "$" + v.commaFormatted }

    static func pct(_ v: Double) -> String { String(format: "%.1f%%", v) }

    /// "likely 26.4–29.6%" when the server sent a range.
    static func range(_ f: PublishReview.Figure) -> String? {
        guard let lo = f.low, let hi = f.high else { return nil }
        return "likely " + String(format: "%.1f\u{2013}%.1f%%", lo, hi)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("The numbers")
            LazyVGrid(columns: [GridItem(.flexible(), spacing: CavnarSpace.s),
                                GridItem(.flexible(), spacing: CavnarSpace.s)],
                      alignment: .leading, spacing: CavnarSpace.s) {
                if let labor = numbers.laborPct, let v = labor.value {
                    tile("Labor", Self.pct(v), sub: Self.range(labor))
                }
                if let cost = numbers.cost, let v = cost.value {
                    tile("Cost", Self.money(v), sub: cost.salaried.map { "+ \(Self.money($0)) salaried" })
                }
                if let ot = numbers.overtimeHours {
                    tile("Overtime", ot > 0 ? "\(ot.commaFormatted)h" : "None",
                         tone: ot > 0 ? .cavnarAmber : .cavnarInk)
                }
                if let sales = numbers.forecastSales, let v = sales.value {
                    tile("Forecast sales", Self.money(v))
                }
            }
            ForEach(Array(trustLines.enumerated()), id: \.offset) { _, line in
                CavnarMixedText(line, role: .secondary)
            }
            if let savings = numbers.savings, let v = savings.value, v > 0 {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    CavnarMixedText("Estimated savings \(Self.money(v))", role: .secondary, color: .cavnarInk)
                    ScheduleRowTag(text: "Projection")
                }
                if let basis = savings.basis { CavnarMixedText(basis, role: .caption) }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    /// The track record and basis under the tiles — the reason to trust
    /// (or not) each figure, in words.
    private var trustLines: [String] {
        var out: [String] = []
        for f in [numbers.laborPct, numbers.cost, numbers.forecastSales].compactMap({ $0 }) {
            if let t = f.trackRecord { out.append(t) }
            if let b = f.basis, !out.contains(b) { out.append(b) }
            if let t = f.text, !out.contains(t) { out.append(t) }
        }
        return out
    }

    private func tile(_ label: String, _ value: String, sub: String? = nil, tone: Color = .cavnarInk) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarKicker(label, tint: .cavnarInk2, isHeader: false)
            Text(value).cavnarText(.figureM, color: tone)
                .minimumScaleFactor(0.85)
                .lineLimit(1)
            if let sub { CavnarMixedText(sub, role: .caption) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(CavnarSpace.s)
        .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
            .fill(Color.cavnarPaper2))
        .accessibilityElement(children: .combine)
    }
}

// MARK: - The proof, one tap away

/// What changed, where the week is fragile, why these people and how sure
/// — each a line on the sheet, its detail behind one tap.
struct PublishReviewProof: View {
    let review: PublishReview
    let busy: String?
    let notes: [String: String]
    let onRun: (PublishReview.Action) -> Void

    @State private var showChanged = false
    @State private var showFragile = false
    @State private var showWhy = false

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.m) {
            if !review.fragile.isEmpty {
                section("Where this week is fragile", count: review.fragile.count, expanded: $showFragile) {
                    ForEach(review.fragile) { line in lineRow(line) }
                }
            }
            if !review.changed.isEmpty {
                section("What changed", count: nil, expanded: $showChanged) {
                    ForEach(review.changed.groups) { group in
                        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                            Text(group.title).cavnarText(.label)
                            ForEach(group.lines.prefix(5)) { line in lineRow(line) }
                        }
                    }
                }
            }
            if !review.why.isEmpty {
                section("Why these people", count: nil, expanded: $showWhy) {
                    ForEach(review.why.assignments.prefix(3)) { line in lineRow(line) }
                    if !review.why.leftOff.isEmpty {
                        Text("Left off this week").cavnarText(.label)
                        ForEach(review.why.leftOff) { p in
                            CavnarMixedText(p.reason.isEmpty ? p.name : "\(p.name): \(p.reason)", role: .secondary)
                        }
                    }
                }
            }
            let sure = review.sure.lines
            if !sure.isEmpty {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarKicker("How sure we are")
                    ForEach(sure, id: \.self) { CavnarMixedText($0, role: .secondary) }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private func section<Content: View>(_ title: String, count: Int?, expanded: Binding<Bool>,
                                        @ViewBuilder content: @escaping () -> Content) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker(count.map { "\(title) \u{00B7} \($0)" } ?? title)
            CavnarEvidenceDisclosure(label: "See " + title.lowercased(), isExpanded: expanded) {
                VStack(alignment: .leading, spacing: CavnarSpace.xs) { content() }
            }
        }
    }

    @ViewBuilder
    private func lineRow(_ line: PublishReview.Line) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarMixedText(line.text, role: .secondary, color: .cavnarInk)
            if let action = line.action {
                Button {
                    onRun(action)
                } label: {
                    Group {
                        if busy == action.id { CavnarShimmerText(text: "Working\u{2026}") } else { Text(action.buttonLabel) }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(busy != nil)
                if let note = notes[action.id] { CavnarMixedText(note, role: .secondary, color: .cavnarRedText) }
            }
        }
    }
}
