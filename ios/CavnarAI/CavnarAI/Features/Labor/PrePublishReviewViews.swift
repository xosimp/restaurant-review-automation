import SwiftUI

// The Send sheet's decision, drawn from the one pre-publish payload
// (PrePublishReview.swift): the verdict first, then what must be fixed (red:
// the must-fix tier — TRUST-24), what to decide before sending (amber) apart
// from the suggestions that never hold it (TRUST-16), the rules the owner
// always sends past (TRUST-10), the numbers with how far to trust them, and —
// one tap away each — what changed, where the week is fragile, why these
// people (the engine's own reason, TRUST-1/2) and how sure (the K1 figure the
// web shows, "—" below the floor — TRUST-6, TRUST-23).
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

// MARK: - Shared

/// One tap the review offers, with its busy state and what it said back.
struct PublishActionButton: View {
    let action: PublishReview.Action
    let busy: String?
    let notes: [String: String]
    var primary = false
    let onRun: (PublishReview.Action) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            Button {
                onRun(action)
            } label: {
                Group {
                    if busy == action.id {
                        CavnarShimmerText(text: "Working\u{2026}")
                    } else {
                        Text(action.buttonLabel)
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .modifier(PublishButtonStyle(primary: primary, disabled: busy != nil))
            .disabled(busy != nil)
            if let note = notes[action.id] {
                CavnarMixedText(note, role: .secondary, color: .cavnarRedText)
            }
        }
    }
}

private struct PublishButtonStyle: ViewModifier {
    let primary: Bool
    let disabled: Bool
    func body(content: Content) -> some View {
        if primary {
            content.buttonStyle(CavnarPrimaryButtonStyle(isDisabled: disabled))
        } else {
            content.buttonStyle(CavnarSecondaryButtonStyle())
        }
    }
}

/// "The law" beside an item the law stands behind (TRUST-4, TRUST-8).
private struct LawTag: View {
    var body: some View { ScheduleRowTag(text: "The law", tone: .cavnarRedText) }
}

// MARK: - Must fix

/// Red: the must-fix tier. One card per rule kind with its one fix (the
/// hard-rule pass, which shows each change — TRUST-15), the items folded
/// under it; each item fixed or sent past on its own with what the gate
/// asks: a law an edit can fix never (TRUST-4), the posting law with the
/// owner's acknowledgement (TRUST-8), a manager gap by a manager or owner on
/// the roster — never a typed name (TRUST-5), anything else with a reason
/// of a few words.
struct PublishMustFixCard: View {
    let items: [PublishReview.MustFix]
    var groups: [PublishReview.Group] = []
    @Binding var overrides: [String: PublishOverrideChoice]
    let busy: String?
    let notes: [String: String]
    /// The gate's refusal of an override given, by key (409
    /// `override_refused`).
    var refused: [String: String] = [:]
    let onRun: (PublishReview.Action) -> Void

    @State private var editing: String?
    @State private var draft = ""
    @State private var coveredBy = ""
    @State private var legalAck = false
    @State private var openGroups: Set<String> = []
    @FocusState private var reasonFocused: Bool

    private var anyHard: Bool { items.contains(where: \.hard) }

    var body: some View {
        let folded = PublishReview.folded(items, groups: groups, key: \.key)
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker(anyHard ? "Must fix before sending \u{00B7} \(items.count)" : "Before this goes out",
                         icon: anyHard ? "xmark.octagon.fill" : "hand.raised.fill",
                         tint: anyHard ? .cavnarRedText : .cavnarAmber)
            // What holds the send, one tap away (text cut 10/17/26): the
            // kicker and each item's own fix say what to do.
            CavnarEvidenceSection(label: "See how it works") {
                Text("The law is fixed or the week doesn\u{2019}t go. A manager gap is covered by a manager or owner on your roster. Anything else is fixed, or sent past on its own with a reason.")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(folded.groups) { f in
                groupBlock(f.group, f.members)
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
            ForEach(folded.rest) { item in
                row(item)
                if item.id != folded.rest.last?.id {
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

    /// "17 people — too many days in a row", its one fix, and each item
    /// behind one tap.
    @ViewBuilder
    private func groupBlock(_ g: PublishReview.Group, _ members: [PublishReview.MustFix]) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Circle().fill(Color.cavnarRed).frame(width: 8, height: 8).accessibilityHidden(true)
                CavnarMixedText(g.text, role: .body, color: .cavnarInk)
                if g.law { LawTag() }
            }
            .accessibilityElement(children: .combine)
            ForEach(Array(g.fixes.enumerated()), id: \.offset) { i, fix in
                PublishActionButton(action: fix, busy: busy, notes: notes, primary: i == 0, onRun: onRun)
            }
            CavnarEvidenceDisclosure(label: "Each one, with its own fix or override",
                                     isExpanded: Binding(get: { openGroups.contains(g.id) || members.contains { $0.key == editing } },
                                                         set: { if $0 { openGroups.insert(g.id) } else { openGroups.remove(g.id) } })) {
                VStack(alignment: .leading, spacing: CavnarSpace.s) {
                    ForEach(members) { item in row(item) }
                }
            }
        }
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
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        CavnarMixedText(item.text, role: .body, color: .cavnarInk)
                        if item.law { LawTag() }
                    }
                    if let before = item.overriddenBefore {
                        CavnarMixedText(before, role: .caption)
                    }
                    if let why = refused[item.key] {
                        CavnarMixedText(why, role: .secondary, color: .cavnarRedText)
                    }
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel((item.hard ? "Must fix: " : "Check: ") + item.text + (item.law ? ". The law." : ""))
            if let reason, PublishGate.complete(item, reason) {
                // Sent past on purpose: said, with the words, and undoable.
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    CavnarMixedText(Self.overrideLine(reason), role: .secondary, color: .cavnarInk2)
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

    /// The override's own button: what sending past this one means.
    static func overrideButtonLabel(_ item: PublishReview.MustFix) -> String {
        if item.needsCoveredBy { return "A manager or owner runs the floor" }
        if item.needsLegalAck { return "Send it late \u{2014} I accept the law\u{2019}s consequence" }
        return "Send anyway: I\u{2019}ll handle it"
    }

    @ViewBuilder
    private func actions(_ item: PublishReview.MustFix) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if !item.overrideAllowed, let why = item.overrideWhy {
                CavnarMixedText(why, role: .secondary, color: .cavnarInk2)
            }
            ForEach(Array(item.fixes.filter { $0.action != "override" }.enumerated()), id: \.element.id) { i, fix in
                PublishActionButton(action: fix, busy: busy, notes: notes, primary: i == 0, onRun: onRun)
            }
            if item.overrideAllowed {
                Button {
                    Haptic.light()
                    draft = ""
                    coveredBy = ""
                    legalAck = false
                    editing = item.key
                    reasonFocused = !item.needsCoveredBy && !item.needsLegalAck
                } label: {
                    Text(Self.overrideButtonLabel(item)).frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            } else if item.fixes.isEmpty && item.overrideWhy == nil {
                CavnarMixedText("This one can\u{2019}t be sent past. Change the week to fix it.",
                                role: .secondary, color: .cavnarInk2)
            }
        }
    }

    /// "You'll send past this: 'reason'" / "Erik runs the floor".
    static func overrideLine(_ c: PublishOverrideChoice) -> String {
        let words = c.reason.trimmingCharacters(in: .whitespacesAndNewlines)
        if let who = c.coveredBy, !who.isEmpty {
            return words.isEmpty || words == "\(who) runs the floor" ? "\(who) runs the floor."
                                                                   : "\(who) runs the floor: \u{201C}\(words)\u{201D}"
        }
        let law = c.legalAck ? " \u{2014} the law acknowledged" : ""
        return words.isEmpty ? "You\u{2019}ll send past this\(law)." : "You\u{2019}ll send past this\(law): \u{201C}\(words)\u{201D}"
    }

    private func reasonField(_ item: PublishReview.MustFix) -> some View {
        let words = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        let who = coveredBy.trimmingCharacters(in: .whitespaces)
        let choice = PublishOverrideChoice(reason: words, coveredBy: who.isEmpty ? nil : who, legalAck: legalAck)
        let ready = PublishGate.complete(item, choice)
        return VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if !item.needsLegalAck {
                Text(item.reasonPrompt).cavnarText(.label)
            }
            if item.needsCoveredBy {
                // Only a manager or owner on the roster who can work that day
                // (TRUST-5): somebody else is put on the week, or named a
                // manager in Team (the item's own fix), never typed here.
                if item.candidates.isEmpty {
                    Text("Nobody on your roster who can work that day is a manager or owner \u{2014} add a manager\u{2019}s shift, or name a manager in Team.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    AccountFlowLayout(spacing: 6) {
                        ForEach(item.candidates, id: \.self) { name in
                            Button {
                                Haptic.selection()
                                coveredBy = name
                            } label: {
                                AccountChip(text: name, muted: coveredBy != name)
                                    .frame(minHeight: 44)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .accessibilityAddTraits(coveredBy == name ? .isSelected : [])
                        }
                    }
                }
            }
            if item.needsLegalAck {
                Toggle(isOn: $legalAck) {
                    CavnarMixedText(item.legalText ?? item.reasonPrompt, role: .body, color: .cavnarInk)
                }
                .tint(Color.cavnarEmber)
                .frame(minHeight: 44)
            }
            TextField(item.needsCoveredBy ? "Anything to note (optional)"
                      : (item.needsLegalAck ? "Why it has to go out now" : "Your reason, in a few words"),
                      text: $draft, axis: .vertical)
                .font(.cavnar(.body))
                .foregroundStyle(Color.cavnarInk)
                .lineLimit(1...4)
                .focused($reasonFocused)
                .padding(CavnarSpace.s)
                .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                    .fill(Color.cavnarPaper2))
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                    .strokeBorder(reasonFocused ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
            if item.needsReason, !words.isEmpty, let problem = item.reasonProblem(words) {
                CavnarMixedText(problem, role: .caption)
            }
            HStack(spacing: CavnarSpace.xs) {
                Button {
                    Haptic.medium()
                    overrides[item.key] = PublishOverrideChoice(reason: String(words.prefix(300)),
                                                                coveredBy: who.isEmpty ? nil : who,
                                                                legalAck: item.needsLegalAck && legalAck)
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

/// One tap each; the scope the answer holds for is on the button. `holds`:
/// these hold the send (amber, "Decide before sending", a rule's group one
/// answer); otherwise they are suggestions that never do (TRUST-16), and an
/// answer is kept on the server (TRUST-17).
struct PublishDecideCard: View {
    let decisions: [PublishReview.Decision]
    var groups: [PublishReview.Group] = []
    var holds = true
    let busy: String?
    let notes: [String: String]
    let answered: [String: String]
    let onRun: (PublishReview.Action, String) -> Void

    @State private var openGroups: Set<String> = []

    var body: some View {
        let folded = PublishReview.folded(decisions, groups: holds ? groups : [], key: \.key)
        let count = folded.groups.count + folded.rest.count
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            if holds {
                CavnarKicker("Decide before sending \u{00B7} \(count)", icon: "arrow.triangle.branch", tint: .cavnarAmber)
                Text("These hold the send.")
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                CavnarKicker("Suggestions \u{00B7} \(count)", icon: "lightbulb")
                Text("None of these hold the send.")
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(folded.groups) { f in
                let group = f.group, members = f.members
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    CavnarMixedText(group.text, role: .body, color: .cavnarInk)
                    if members.allSatisfy({ answered[$0.key] != nil }) {
                        answeredLine(answered[members[0].key] ?? "")
                    } else {
                        ForEach(Array(group.fixes.enumerated()), id: \.offset) { i, fix in
                            PublishActionButton(action: fix, busy: busy, notes: notes, primary: i == 0) {
                                onRun($0, group.kind)
                            }
                        }
                    }
                    CavnarEvidenceDisclosure(label: "Each one",
                                             isExpanded: Binding(get: { openGroups.contains(group.id) },
                                                                 set: { if $0 { openGroups.insert(group.id) } else { openGroups.remove(group.id) } })) {
                        VStack(alignment: .leading, spacing: CavnarSpace.s) {
                            ForEach(members) { d in decision(d) }
                        }
                    }
                }
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
            ForEach(folded.rest) { d in
                decision(d)
                if d.id != folded.rest.last?.id {
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private func answeredLine(_ answer: String) -> some View {
        HStack(spacing: CavnarSpace.xxs) {
            Image(systemName: "checkmark").foregroundStyle(Color.cavnarGreen)
                .accessibilityHidden(true)
            CavnarMixedText(answer, role: .secondary)
        }
    }

    @ViewBuilder
    private func decision(_ d: PublishReview.Decision) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarMixedText(d.text, role: .body, color: .cavnarInk)
            if let before = d.overriddenBefore {
                CavnarMixedText(before, role: .caption)
            }
            if d.holds && answered[d.key] == nil {
                Text("Send waits for your answer.").cavnarText(.caption)
            }
            if let answer = answered[d.key] {
                answeredLine(answer)
            } else {
                ForEach(d.options) { option in
                    PublishActionButton(action: option, busy: busy, notes: notes) { onRun($0, d.key) }
                }
            }
        }
    }
}

// MARK: - Rules always sent past

/// "You've sent past “too many days in a row” in 9 of 9 weeks — change the
/// rule?" with the one-tap change (TRUST-10).
struct PublishAlwaysOverriddenCard: View {
    let items: [PublishReview.AlwaysOverridden]
    let busy: String?
    let notes: [String: String]
    let onRun: (PublishReview.Action) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Rules you always send past")
            Text("Worth changing \u{2014} or keeping on purpose.")
                .cavnarText(.caption)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(items) { item in
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    CavnarMixedText(item.text, role: .body, color: .cavnarInk)
                    ForEach(Array(item.actions.enumerated()), id: \.offset) { i, a in
                        PublishActionButton(action: a, busy: busy, notes: notes, primary: i == 0, onRun: onRun)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}

// MARK: - Numbers

/// Labor %, cost, overtime and forecast sales, each with how far to trust
/// it; a labor % that cannot be worked out says why and the real fix
/// (TRUST-29); the gap to recent labor is a projection and says so — never
/// summed into anything, never called savings (TRUST-23).
struct PublishNumbersCard: View {
    let numbers: PublishReview.Numbers
    var quality: PublishReview.Quality? = nil
    var busy: String? = nil
    var notes: [String: String] = [:]
    var onRun: ((PublishReview.Action) -> Void)? = nil

    /// " · typical for you" — graded against the restaurant's own weeks.
    static func gradeWord(_ g: String) -> String? {
        switch g {
        case "good": return " \u{00B7} better than your usual week"
        case "typical": return " \u{00B7} typical for you"
        case "weak": return " \u{00B7} weaker than your usual week"
        default: return nil
        }
    }

    static func money(_ v: Double) -> String { "$" + v.commaFormatted }

    static func pct(_ v: Double) -> String { String(format: "%.1f%%", v) }

    /// "likely 26.4–29.6%" when the server sent a range.
    static func range(_ f: PublishReview.Figure) -> String? {
        guard let lo = f.low, let hi = f.high else { return nil }
        return "likely " + String(format: "%.1f\u{2013}%.1f%%", lo, hi)
    }

    /// The labor tile's tone: the server's one grade (TRUST-7) — over at any
    /// amount past the target, neutral with none.
    static func laborTone(_ grade: String?) -> Color {
        grade == "over" ? .cavnarRedText : (grade == "under" ? .cavnarGreen : .cavnarInk)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("The numbers")
            LazyVGrid(columns: [GridItem(.flexible(), spacing: CavnarSpace.s),
                                GridItem(.flexible(), spacing: CavnarSpace.s)],
                      alignment: .leading, spacing: CavnarSpace.s) {
                if let labor = numbers.laborPct, let v = labor.value {
                    tile(labor.label ?? "Labor", Self.pct(v), sub: Self.range(labor) ?? labor.target.map { "target \(Self.pct($0))" },
                         tone: Self.laborTone(labor.grade))
                } else if numbers.laborMissing != nil {
                    tile("Labor", "\u{2014}", tone: .cavnarInk3)
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
            if let missing = numbers.laborMissing {
                CavnarMixedText(missing.text, role: .secondary, color: .cavnarInk2)
                if let action = missing.action, let onRun {
                    PublishActionButton(action: action, busy: busy, notes: notes, onRun: onRun)
                }
            }
            ForEach(Array(trustLines.enumerated()), id: \.offset) { _, line in
                CavnarMixedText(line, role: .secondary)
            }
            if let ot = numbers.overtimeText { CavnarMixedText(ot, role: .secondary) }
            if let reconcile = numbers.forecastSales?.reconcile { CavnarMixedText(reconcile, role: .caption) }
            if let score = quality?.score {
                CavnarMixedText("Shift quality \(score)/100" + (quality?.grade.flatMap(Self.gradeWord) ?? ""),
                                role: .secondary, color: .cavnarInk)
                if let lead = quality?.lead ?? quality?.text { CavnarMixedText(lead, role: .caption) }
                // How much was measured (LOGIC-15) and what the score has
                // meant here (TRUST-31) — "—" below 3 weeks a side.
                if let m = quality?.measuredText { CavnarMixedText(m, role: .caption) }
                if let q = quality { CavnarMixedText(q.outcomeLine, role: .caption) }
            }
            // Scheduled against scheduled (TRUST-7, TRUST-23): this draft
            // against the last published week's plan, a projection — never
            // called savings, withheld while a shift is short.
            if let vp = numbers.vsLastPlan {
                if let v = vp.value {
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        CavnarMixedText("\(vp.label) by \(Self.money(abs(v)))", role: .secondary, color: .cavnarInk)
                        ScheduleRowTag(text: "Projection")
                    }
                    if let t = vp.text { CavnarMixedText(t, role: .caption) }
                } else if let w = vp.withheld {
                    CavnarMixedText(w, role: .caption)
                }
            }
            if let last = numbers.lastWeekText { CavnarMixedText(last, role: .caption) }
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
            if let b = f.basis, !out.contains(b), !["all_in", "hourly"].contains(b) { out.append(b) }
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
    @State private var showFyi = false
    @State private var showingConfidence = false

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
                    // How the draft was made and whether the engine's own
                    // reasons were kept (TRUST-2) — said, never reconstructed.
                    if let draft = review.why.draftText { CavnarMixedText(draft, role: .secondary) }
                    if !review.why.assignments.isEmpty {
                        Text("What Cavnar AI weighed").cavnarText(.label)
                        if let engine = review.why.engineText { CavnarMixedText(engine, role: .caption) }
                        ForEach(review.why.assignments) { a in assignmentRow(a) }
                    }
                    if !review.why.leftOff.isEmpty {
                        Text("Left off this week").cavnarText(.label)
                        ForEach(review.why.leftOff) { p in
                            CavnarMixedText(p.reason.isEmpty ? p.name : "\(p.name): \(p.reason)", role: .secondary)
                        }
                    }
                }
            }
            if !review.fyi.isEmpty {
                section("Worth a look", count: review.fyi.count, expanded: $showFyi) {
                    ForEach(review.fyi) { line in lineRow(line) }
                }
            }
            howSure
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    /// "How sure" — how well supported, over how the published weeks RAN;
    /// "—" until enough of them are measured, with what it rests on (the
    /// web's figure, TRUST-6, TRUST-23). The K1 dimensions behind "Why?".
    private var howSure: some View {
        let cf = review.sure.confidence ?? PublishReview.Sure.Confidence()
        return VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarKicker("How sure we are")
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Text("How sure").cavnarText(.label)
                Spacer(minLength: CavnarSpace.xs)
                Text(cf.figure).cavnarText(.figureS, color: cf.pct == nil ? .cavnarInk3 : .cavnarInk)
                if cf.k1 != nil {
                    Button {
                        Haptic.light()
                        showingConfidence = true
                    } label: {
                        Text("Why?").font(.cavnarBody(CavnarType.caption, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Shows what this confidence rests on")
                }
            }
            .accessibilityElement(children: .combine)
            CavnarMixedText(cf.basisLine, role: .caption)
            if let meaning = cf.meaning { CavnarMixedText(meaning + ".", role: .caption) }
            ForEach(review.sure.lines, id: \.self) { CavnarMixedText($0, role: .secondary) }
        }
        .sheet(isPresented: $showingConfidence) {
            if let k1 = cf.k1 { ConfidenceWhySheet(confidence: k1) }
        }
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

    /// One "why": the reason, where it came from ("Cavnar AI's reason" /
    /// "Worked out after your edits") and, when a stronger runner-up could
    /// take it, the swap (TRUST-1).
    @ViewBuilder
    private func assignmentRow(_ a: PublishReview.Assignment) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                CavnarMixedText(a.text, role: .secondary, color: .cavnarInk)
                if let tag = a.basisLabel { ScheduleRowTag(text: tag) }
            }
            if let swap = a.swap {
                PublishActionButton(action: swap, busy: busy, notes: notes, onRun: onRun)
            }
        }
    }

    @ViewBuilder
    private func lineRow(_ line: PublishReview.Line) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarMixedText(line.text, role: .secondary, color: .cavnarInk)
            if let action = line.action {
                PublishActionButton(action: action, busy: busy, notes: notes, onRun: onRun)
            }
        }
    }
}
