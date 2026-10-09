import SwiftUI
import SafariServices

// The iPhone answer kit (iOS readability round, 10/8/26).
//
// "Web explains. iPhone decides." Every phone screen answers what happened,
// why it matters and what to do next within five seconds; proof sits behind
// a tap; deep analysis and configuration live on the web. Four layers:
//   L0  the glance answer         — CavnarAnswerCard's headline + summary
//   L1  one action in thumb reach — its action slot, or CavnarPinnedBar
//   L2  tap for proof             — its "See the evidence" disclosure,
//                                   CavnarMoreDisclosure for capped lists
//   L3  the web                   — CavnarWebLinkRow
// DESIGN_SYSTEM.md §2 (iOS roles) and §12 (iOS answer kit) hold the rules.

// MARK: - Kicker

/// The one kicker: uppercase, Apfel Fett 12, +1.2 tracking, Ember2. Five
/// hand-rolled kickers in three colours preceded it.
///
///     CavnarKicker("Last night")
///     CavnarKicker("Looking ahead", icon: "calendar.badge.clock", tint: .cavnarAmber)
///
/// `tint` is for a kicker whose colour IS its meaning (an amber projection);
/// everything else stays Ember2.
///
/// `isHeader` (default true) puts the kicker in VoiceOver's headings rotor —
/// right for a kicker over a section. A kicker that labels a tile's figure
/// ("LABOR" over 28.4%) passes false, so the rotor lists sections, not
/// every tile (re-audit S10, 10/8/26).
struct CavnarKicker: View {
    let text: String
    var icon: String? = nil
    var tint: Color = .cavnarEmber2
    var isHeader: Bool = true

    init(_ text: String, icon: String? = nil, tint: Color = .cavnarEmber2, isHeader: Bool = true) {
        self.text = text
        self.icon = icon
        self.tint = tint
        self.isHeader = isHeader
    }

    var body: some View {
        HStack(spacing: CavnarSpace.xxs + 1) {
            if let icon {
                Image(systemName: icon).accessibilityHidden(true)
            }
            Text(text)
        }
        .cavnarText(.kicker, color: tint)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(isHeader ? .isHeader : [])
    }
}

// MARK: - Mixed text by role

extension HomeMixedText {
    /// A sentence in a role's face with every number run in Space Grotesk at
    /// the same size, scaling with the same text style — words and figures
    /// move together under Dynamic Type. Returns `Text`, so it concatenates;
    /// on a view, prefer `CavnarMixedText`, which adds the role's leading.
    ///
    ///     HomeMixedText.make("Labor ran 31% against 28%", role: .body)
    static func make(_ string: String, role: CavnarText, color: Color? = nil,
                     numberColor: Color? = nil) -> Text {
        let ink = color ?? role.defaultColor
        let figureWeight: CGFloat = role.postScriptName.hasSuffix("Fett") ? 700 : 600
        var result = Text(verbatim: "")
        for run in runs(string) {
            if run.isNumber {
                result = result + Text(verbatim: run.text)
                    .font(.cavnarNumber(role.size, weight: figureWeight, relativeTo: role.textStyle))
                    .foregroundStyle(numberColor ?? ink)
            } else {
                result = result + Text(verbatim: run.text)
                    .font(.cavnar(role))
                    .foregroundStyle(ink)
            }
        }
        return result
    }
}

/// A mixed words-and-figures sentence set in a role — the view form of
/// `HomeMixedText.make(_:role:)`, with the role's leading and tracking.
///
///     CavnarMixedText("Food cost is 34.2%, 2.1 points over", role: .lead)
///
/// A `.lead` or `.body` sentence is a paragraph to READ, so it follows the
/// phone's text size past the app's `.xxxLarge` cap, up to
/// `.accessibility2` (`cavnarReadingSize`; re-audit S4, 10/8/26). Pass
/// `readingSize: false` for one that sits beside other content in a row
/// that would not survive an accessibility size.
struct CavnarMixedText: View {
    let text: String
    var role: CavnarText = .body
    var color: Color? = nil
    var numberColor: Color? = nil
    var readingSize: Bool = true

    init(_ text: String, role: CavnarText = .body, color: Color? = nil, numberColor: Color? = nil,
         readingSize: Bool = true) {
        self.text = text
        self.role = role
        self.color = color
        self.numberColor = numberColor
        self.readingSize = readingSize
    }

    /// The roles that are reading paragraphs.
    static func readsPastCap(_ role: CavnarText) -> Bool { role == .lead || role == .body }

    var body: some View {
        let text = HomeMixedText.make(self.text, role: role, color: color, numberColor: numberColor)
            .cavnarText(role, color: color)
            .fixedSize(horizontal: false, vertical: true)
        if readingSize && Self.readsPastCap(role) {
            text.cavnarReadingSize(upTo: .accessibility2)
        } else {
            text
        }
    }
}

// MARK: - Answer card

/// The one anatomy for every AI card on the iPhone. Every part but the
/// headline is optional and collapses when absent:
///
///     CavnarAnswerCard(
///         kicker: "Labor",
///         headline: "Labor ran 31% last night, 3 points over.",
///         summary: "Two closers stayed past 11pm with 14 covers left.",
///         cause: "Closers were scheduled to 12am on a slow Tuesday.",
///         isHypothesis: true,
///         alternativeCause: "a late private party",
///         expectedOutcome: "If you cut one closer to 10pm, about $85 a night.",
///         confidence: rec.confidence.map {
///             ConfidenceLine(confidence: $0, recKey: rec.key, surface: "labor", module: "labor")
///         }
///     ) {
///         Button("Adjust Tuesday") { … }.buttonStyle(CavnarPrimaryButtonStyle())
///         RecAnswerRow(key: rec.key, surface: "labor")
///     } detail: {
///         LaborEvidenceList(rows: rec.evidence)
///     }
///
/// - headline: one sentence — `.lead` (default) or `.headline` for a hero.
/// - summary: one sentence of why it matters (Body).
/// - cause: one line. `isHypothesis` labels it "Hypothesis" in amber — use
///   it whenever no measured signal backs the cause.
/// - alternativeCause: ONE other reading, shown as "Could also be: …".
/// - actions: the one primary button and/or the answer row. Nothing else.
/// - expectedOutcome: one line; conditional wording ("If you …, about …")
///   is the caller's job — never promise a measured result.
/// - confidence: pass a `ConfidenceLine` through unchanged.
/// - detail: the proof, collapsed behind `detailLabel` ("See the evidence").
struct CavnarAnswerCard<Actions: View, Detail: View>: View {
    var kicker: String? = nil
    let headline: String
    var headlineRole: CavnarText = .lead
    var summary: String? = nil
    var cause: String? = nil
    var isHypothesis: Bool = false
    var alternativeCause: String? = nil
    var expectedOutcome: String? = nil
    var confidence: ConfidenceLine? = nil
    var detailLabel: String = "See the evidence"
    /// The card's surface; `nil` draws no card (the caller already has one).
    var surface: CavnarSurface? = .ai
    @ViewBuilder var actions: () -> Actions
    @ViewBuilder var detail: () -> Detail

    @State private var showingDetail = false

    var body: some View {
        let stack = VStack(alignment: .leading, spacing: CavnarSpace.s) {
            if let kicker, !kicker.isEmpty {
                CavnarKicker(kicker)
            }
            // L0 is read, not scanned: headline and summary follow the
            // phone's text size past the app's cap (re-audit S4).
            CavnarMixedText(headline, role: headlineRole)
                .cavnarReadingSize(upTo: .accessibility2)
                .accessibilityAddTraits(.isHeader)
            if let summary = Self.present(summary) {
                CavnarMixedText(summary, role: .body)
            }
            if let cause = Self.present(cause) {
                causeLine(cause)
            }
            if let alt = Self.present(alternativeCause) {
                // Wraps in full: a tail-clipped alternative read as a
                // different claim (re-audit S7). Ink2 lead-in — it is read.
                (Text("Could also be: ").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2)
                    + HomeMixedText.make(alt, role: .secondary))
                    .lineSpacing(CavnarText.secondary.lineSpacing)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if Actions.self != EmptyView.self {
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    actions()
                }
                .padding(.top, CavnarSpace.xxs)
            }
            if let outcome = Self.present(expectedOutcome) {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    Image(systemName: "arrow.up.forward")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarEmber2)
                        .accessibilityHidden(true)
                    CavnarMixedText(outcome, role: .secondary)
                }
                .accessibilityElement(children: .combine)
            }
            if let confidence {
                confidence
            }
            if Detail.self != EmptyView.self {
                CavnarEvidenceDisclosure(label: detailLabel, isExpanded: $showingDetail, content: detail)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)

        if let surface {
            stack.cavnarCard(surface)
        } else {
            stack
        }
    }

    private func causeLine(_ cause: String) -> some View {
        let label = isHypothesis ? "Hypothesis \u{00B7} " : "Why: "
        return (Text(label)
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(isHypothesis ? Color.cavnarAmber : Color.cavnarInk2)
                + HomeMixedText.make(cause, role: .secondary))
            .lineSpacing(CavnarText.secondary.lineSpacing)
            .fixedSize(horizontal: false, vertical: true)
    }

    private static func present(_ s: String?) -> String? {
        guard let t = s?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty else { return nil }
        return t
    }
}

// MARK: - Evidence disclosure

/// L2 — the proof behind one tap: "See the evidence ⌄" / "Hide the
/// evidence ⌃", 44pt, Ember2, collapsed by default. `CavnarAnswerCard`
/// draws it for its `detail`; a screen with its own anatomy (the AI
/// consultant sheet) uses it directly.
///
///     CavnarEvidenceDisclosure(label: "See the evidence") { EvidenceList(rows) }
///
/// The hide label is derived from the show label — "Show the reasoning" →
/// "Hide the reasoning" — so the two always name the same thing (re-audit
/// S7: it always said "Hide the evidence").
struct CavnarEvidenceDisclosure<Content: View>: View {
    var label: String = "See the evidence"
    @Binding var isExpanded: Bool
    @ViewBuilder var content: () -> Content

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(label: String = "See the evidence", isExpanded: Binding<Bool>,
         @ViewBuilder content: @escaping () -> Content) {
        self.label = label
        self._isExpanded = isExpanded
        self.content = content
    }

    /// "See the evidence" → "Hide the evidence"; "Show the reasoning" →
    /// "Hide the reasoning"; "Why?" → "Hide". A leading show-verb (See,
    /// Show, Read, View, Open) becomes "Hide"; a trailing "›" or "…" goes.
    static func hideLabel(for label: String) -> String {
        var t = label.trimmingCharacters(in: .whitespaces)
        for suffix in ["\u{203A}", "\u{2026}", "...", ">"] where t.hasSuffix(suffix) {
            t = String(t.dropLast(suffix.count)).trimmingCharacters(in: .whitespaces)
        }
        let words = t.split(separator: " ", maxSplits: 1).map(String.init)
        let verbs: Set<String> = ["see", "show", "read", "view", "open"]
        if words.count == 2, verbs.contains(words[0].lowercased()) {
            return "Hide " + words[1]
        }
        return "Hide"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            Button {
                Haptic.light()
                if reduceMotion {
                    isExpanded.toggle()
                } else {
                    withAnimation(.easeOut(duration: 0.22)) { isExpanded.toggle() }
                }
            } label: {
                HStack(spacing: CavnarSpace.xxs + 2) {
                    Text(isExpanded ? Self.hideLabel(for: label) : label)
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    Image(systemName: "chevron.down")
                        .font(.cavnar(.caption))
                        .rotationEffect(.degrees(isExpanded ? 180 : 0))
                        .accessibilityHidden(true)
                    Spacer(minLength: 0)
                }
                .foregroundStyle(Color.cavnarEmber2)
                .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")
            if isExpanded {
                content()
                    .transition(reduceMotion ? .identity : .opacity)
            }
        }
    }
}

/// Owns its own open state — for a screen that just wants the disclosure.
struct CavnarEvidenceSection<Content: View>: View {
    var label: String = "See the evidence"
    @ViewBuilder var content: () -> Content
    @State private var expanded = false

    var body: some View {
        CavnarEvidenceDisclosure(label: label, isExpanded: $expanded, content: content)
    }
}

extension CavnarAnswerCard where Detail == EmptyView {
    init(kicker: String? = nil, headline: String, headlineRole: CavnarText = .lead, summary: String? = nil,
         cause: String? = nil, isHypothesis: Bool = false, alternativeCause: String? = nil,
         expectedOutcome: String? = nil, confidence: ConfidenceLine? = nil,
         surface: CavnarSurface? = .ai, @ViewBuilder actions: @escaping () -> Actions) {
        self.init(kicker: kicker, headline: headline, headlineRole: headlineRole, summary: summary,
                  cause: cause, isHypothesis: isHypothesis, alternativeCause: alternativeCause,
                  expectedOutcome: expectedOutcome, confidence: confidence, surface: surface,
                  actions: actions, detail: { EmptyView() })
    }
}

extension CavnarAnswerCard where Actions == EmptyView {
    init(kicker: String? = nil, headline: String, headlineRole: CavnarText = .lead, summary: String? = nil,
         cause: String? = nil, isHypothesis: Bool = false, alternativeCause: String? = nil,
         expectedOutcome: String? = nil, confidence: ConfidenceLine? = nil,
         detailLabel: String = "See the evidence", surface: CavnarSurface? = .ai,
         @ViewBuilder detail: @escaping () -> Detail) {
        self.init(kicker: kicker, headline: headline, headlineRole: headlineRole, summary: summary,
                  cause: cause, isHypothesis: isHypothesis, alternativeCause: alternativeCause,
                  expectedOutcome: expectedOutcome, confidence: confidence, detailLabel: detailLabel,
                  surface: surface, actions: { EmptyView() }, detail: detail)
    }
}

extension CavnarAnswerCard where Actions == EmptyView, Detail == EmptyView {
    init(kicker: String? = nil, headline: String, headlineRole: CavnarText = .lead, summary: String? = nil,
         cause: String? = nil, isHypothesis: Bool = false, alternativeCause: String? = nil,
         expectedOutcome: String? = nil, confidence: ConfidenceLine? = nil,
         surface: CavnarSurface? = .ai) {
        self.init(kicker: kicker, headline: headline, headlineRole: headlineRole, summary: summary,
                  cause: cause, isHypothesis: isHypothesis, alternativeCause: alternativeCause,
                  expectedOutcome: expectedOutcome, confidence: confidence, surface: surface,
                  actions: { EmptyView() }, detail: { EmptyView() })
    }
}

// MARK: - Web link row

/// L3 — "Alert rules · Edit on the web ›": one 44pt row that opens the web
/// dashboard at a nav path, in an in-app browser sheet.
///
///     CavnarWebLinkRow(title: "Alert rules", path: "account/notifications")
///     CavnarWebLinkRow(title: "Pars and counts", subtitle: "Set every par on the web",
///                      path: "inventory/pars")
///
/// `path` is a nav path (nav.py; the dashboard's `cavNav`), opened as
/// https://dashboard.cavnar.ai/?nav=<path> — the same link every email and
/// push carries (`CavnarHandoff.webpageURL`). Valid heads: home, reviews,
/// labor, inventory, marketing, intel, dsr, ask, recs, account; sections the
/// dashboard marks with data-nav, e.g. labor/schedule, labor/people,
/// labor/requests, labor/tasks, inventory/invoices, inventory/order,
/// inventory/pars, inventory/menu, inventory/waste, inventory/count,
/// marketing/opportunities, marketing/guests, marketing/newsletter,
/// marketing/website, intel/visibility, inventory/margins,
/// marketing/attribution (the Analytics tab's "What posts did to sales"),
/// reviews/inbox, reviews/analytics, home/results, account/overview,
/// account/restaurant, account/notifications (alert rules),
/// account/automation, account/integrations, account/people,
/// account/billing, account/security, account/data, account/support.
/// An unknown section opens its module, never an error.
///
/// Why an in-app browser (SFSafariViewController) and not `openURL`: the app
/// claims dashboard.cavnar.ai's "/" with any ?nav= or #fragment as a
/// universal link (hosted_dashboard.apple_app_site_association), so
/// `openURL` would route straight back into the app. The in-app browser
/// keeps its own sign-in, so the owner signs in to the web once there.
struct CavnarWebLinkRow: View {
    let title: String
    var subtitle: String? = nil
    let path: String
    var actionLabel: String = "Edit on the web"

    @State private var presented: WebTarget?

    private struct WebTarget: Identifiable {
        let url: URL
        var id: String { url.absoluteString }
    }

    var body: some View {
        Button {
            Haptic.light()
            presented = CavnarHandoff.webpageURL(for: Self.navPath(path)).map(WebTarget.init(url:))
        } label: {
            HStack(alignment: .center, spacing: CavnarSpace.s) {
                Image(systemName: "safari")
                    .font(.cavnar(.body))
                    .foregroundStyle(Color.cavnarInk3)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    (Text(title).font(.cavnar(.label)).foregroundStyle(Color.cavnarInk)
                        + Text(" \u{00B7} \(actionLabel)").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarEmber2))
                        .fixedSize(horizontal: false, vertical: true)
                    if let subtitle, !subtitle.isEmpty {
                        Text(subtitle)
                            .cavnarText(.caption)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
                    .accessibilityHidden(true)
            }
            .padding(.vertical, CavnarSpace.xs)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        // The subtitle is part of the row's meaning ("Set every par on the
        // web"); VoiceOver used to stop at the action (re-audit S10).
        .accessibilityLabel(Self.accessibilityText(title: title, subtitle: subtitle, actionLabel: actionLabel))
        .accessibilityHint("Opens the Cavnar AI web dashboard")
        .sheet(item: $presented) { target in
            CavnarSafariView(url: target.url)
                .ignoresSafeArea()
        }
    }

    /// "Alert rules. Edit on the web. Choose who hears what" — title, the
    /// action, then the subtitle when there is one.
    static func accessibilityText(title: String, subtitle: String?, actionLabel: String) -> String {
        var parts = [title, actionLabel]
        if let subtitle = subtitle?.trimmingCharacters(in: .whitespacesAndNewlines), !subtitle.isEmpty {
            parts.append(subtitle)
        }
        return parts.joined(separator: ". ")
    }

    /// "/#account/notifications", "#labor", "/labor/schedule" → the bare nav
    /// path the dashboard routes.
    static func navPath(_ raw: String) -> String {
        var p = raw.trimmingCharacters(in: .whitespaces)
        while let first = p.first, first == "/" || first == "#" { p.removeFirst() }
        return p
    }
}

/// The in-app browser behind `CavnarWebLinkRow`, ember-tinted.
struct CavnarSafariView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        let config = SFSafariViewController.Configuration()
        config.entersReaderIfAvailable = false
        let vc = SFSafariViewController(url: url, configuration: config)
        vc.preferredControlTintColor = UIColor(Color.cavnarEmber2)
        vc.preferredBarTintColor = UIColor(Color.cavnarPaper)
        vc.dismissButtonStyle = .done
        return vc
    }

    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}

// MARK: - Pinned bar

/// L1 — a screen's primary action pinned in thumb reach above the home
/// indicator, in the look of Labor's send bar and the review detail's reply
/// bar: Paper at 96%, a hairline on top, the screen gutter at the sides.
///
///     ScrollView { … }
///         .cavnarPinnedBar {
///             Button { send() } label: { Text("Send to staff").frame(maxWidth: .infinity) }
///                 .buttonStyle(CavnarPrimaryButtonStyle())
///         }
///
/// One primary per bar; a secondary may sit beside it. `note` is an
/// optional line above the buttons ("Save your changes first"), and
/// `noteTone` says what kind of line it is (re-audit S6, 10/8/26 — every
/// note used to be a grey caption, so a failed save or a blocking warning
/// read as a hint):
/// - `.hint` (default) — caption, Ink3: guidance.
/// - `.warning` — secondary, Amber, with a warning glyph: something will
///   block or change the action.
/// - `.error` — secondary, `cavnarRedText`, with an error glyph: the last
///   attempt failed. VoiceOver announces a warning or an error.
enum CavnarNoteTone: Sendable {
    case hint, warning, error
}

struct CavnarPinnedBar<Content: View>: View {
    var note: String? = nil
    var noteTone: CavnarNoteTone = .hint
    @ViewBuilder var content: () -> Content

    init(note: String? = nil, noteTone: CavnarNoteTone = .hint, @ViewBuilder content: @escaping () -> Content) {
        self.note = note
        self.noteTone = noteTone
        self.content = content
    }

    var body: some View {
        VStack(spacing: CavnarSpace.xs) {
            if let note, !note.isEmpty {
                noteLine(note)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .onAppear { announce(note) }
                    .onChange(of: note) { _, new in announce(new) }
            }
            HStack(spacing: CavnarSpace.s) {
                content()
            }
        }
        .padding(.horizontal, CavnarSpace.gutter)
        .padding(.top, CavnarSpace.s)
        .padding(.bottom, CavnarSpace.xs)
        .frame(maxWidth: .infinity)
        .background(Color.cavnarPaper.opacity(0.96))
        .overlay(alignment: .top) { Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1) }
    }

    @ViewBuilder
    private func noteLine(_ note: String) -> some View {
        switch noteTone {
        case .hint:
            Text(note).cavnarText(.caption)
        case .warning, .error:
            let isError = noteTone == .error
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Image(systemName: isError ? "xmark.circle.fill" : "exclamationmark.triangle.fill")
                    .accessibilityHidden(true)
                Text(note)
            }
            .cavnarText(.secondary, color: isError ? .cavnarRedText : .cavnarAmber)
            .accessibilityElement(children: .combine)
            .accessibilityLabel((isError ? "Error: " : "Warning: ") + note)
        }
    }

    private func announce(_ note: String) {
        guard noteTone != .hint, !note.isEmpty else { return }
        AccessibilityNotification.Announcement(note).post()
    }
}

extension View {
    /// Pins `content` to the bottom safe area as a `CavnarPinnedBar`; the
    /// scroll content above insets itself so nothing hides under the bar.
    func cavnarPinnedBar<Content: View>(note: String? = nil, noteTone: CavnarNoteTone = .hint,
                                        @ViewBuilder _ content: @escaping () -> Content) -> some View {
        safeAreaInset(edge: .bottom, spacing: 0) {
            CavnarPinnedBar(note: note, noteTone: noteTone, content: content)
        }
    }
}

// MARK: - More disclosure

/// L2 for a capped list: "+3 more" (or "Show all 8") under the first few
/// rows, revealing the rest in place; "Show less" folds them back. 44pt.
///
///     ForEach(items.prefix(3)) { RowView($0) }
///     if items.count > 3 {
///         CavnarMoreDisclosure(hiddenCount: items.count - 3) {
///             ForEach(items.dropFirst(3)) { RowView($0) }
///         }
///     }
///
/// Pass `total` to say "Show all N" instead of "+N more".
struct CavnarMoreDisclosure<Content: View>: View {
    let hiddenCount: Int
    var total: Int? = nil
    @ViewBuilder var content: () -> Content

    @State private var expanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        if hiddenCount > 0 {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if expanded {
                    content()
                        .transition(reduceMotion ? .identity : .opacity)
                }
                CavnarMoreToggle(hiddenCount: hiddenCount, total: total, isExpanded: $expanded)
            }
        }
    }
}

/// The 44pt toggle `CavnarMoreDisclosure` draws, for a list that manages its
/// own cap (`items.prefix(expanded ? items.count : 3)`).
struct CavnarMoreToggle: View {
    let hiddenCount: Int
    var total: Int? = nil
    @Binding var isExpanded: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var label: String {
        if isExpanded { return "Show less" }
        if let total { return "Show all \(total)" }
        return "+\(hiddenCount) more"
    }

    var body: some View {
        Button {
            Haptic.light()
            if reduceMotion {
                isExpanded.toggle()
            } else {
                withAnimation(.easeOut(duration: 0.22)) { isExpanded.toggle() }
            }
        } label: {
            HStack(spacing: CavnarSpace.xxs + 2) {
                HomeMixedText.make(label, role: .label, color: .cavnarEmber2)
                Image(systemName: "chevron.down")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarEmber2)
                    .rotationEffect(.degrees(isExpanded ? 180 : 0))
                    .accessibilityHidden(true)
                Spacer(minLength: 0)
            }
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")
    }
}
