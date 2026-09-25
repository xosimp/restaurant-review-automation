import SwiftUI
import Observation

// MARK: - Store

/// The reads of GET /mobile/api/benchmarks/card the app keeps, one per
/// module (and "home" for the strip). A module screen reuses whatever
/// landed in the last five minutes rather than asking again on every open;
/// everything is dropped when the session (user or location) changes, so
/// one restaurant's comparison is never shown under another. The engine
/// behind it is deterministic — no model call — so a re-read is cheap.
@Observable
@MainActor
final class BenchmarkCardStore {
    static let shared = BenchmarkCardStore()

    private(set) var cards: [String: BenchmarkCard] = [:]
    private(set) var locations: LocationComparison?
    @ObservationIgnored private var loadedAt: [String: Date] = [:]
    @ObservationIgnored private var locationsLoadedAt: Date?
    @ObservationIgnored private var inFlight: Set<String> = []
    @ObservationIgnored private var generation = SessionScope.generation
    @ObservationIgnored private let client: APIClient
    static let reuseFor: TimeInterval = 300

    init(client: APIClient = .shared) { self.client = client }

    private func adoptCurrentSession() {
        guard generation != SessionScope.generation else { return }
        generation = SessionScope.generation
        cards = [:]; loadedAt = [:]; locations = nil; locationsLoadedAt = nil; inFlight = []
    }

    /// The card for a module, or nil — never another session's.
    func card(for module: String) -> BenchmarkCard? {
        generation == SessionScope.generation ? cards[module] : nil
    }

    /// The location comparison, or nil — never another session's.
    var currentLocations: LocationComparison? {
        generation == SessionScope.generation ? locations : nil
    }

    func load(_ module: String, force: Bool = false) async {
        adoptCurrentSession()
        if !force, let at = loadedAt[module], Date().timeIntervalSince(at) < Self.reuseFor { return }
        guard !inFlight.contains(module) else { return }
        inFlight.insert(module)
        defer { inFlight.remove(module) }
        let mine = generation
        do {
            let card: BenchmarkCard = try await client.send("/mobile/api/benchmarks/card", query: ["module": module],
                                                            hapticOnError: false)
            guard mine == SessionScope.generation else { return }
            cards[module] = card
            loadedAt[module] = Date()
        } catch {
            // A login that cannot see the module (400), a cancelled read or a
            // server without the card: draw nothing rather than an error on
            // a screen whose own figures loaded fine.
            guard mine == SessionScope.generation else { return }
            if !(error is CancellationError) { cards[module] = nil }
        }
    }

    /// Re-read every card already on screen — after the owner confirms the
    /// restaurant profile, the card must not keep saying "isn't confirmed"
    /// for five minutes (Benchmarking #33, R3-25).
    func reloadAll() async {
        adoptCurrentSession()
        let loaded = Array(cards.keys)
        for module in loaded {
            await load(module, force: true)
        }
    }

    func loadLocations(force: Bool = false) async {
        adoptCurrentSession()
        if !force, let at = locationsLoadedAt, Date().timeIntervalSince(at) < Self.reuseFor { return }
        let mine = generation
        do {
            let lc: LocationComparison = try await client.send("/mobile/api/benchmarks/locations",
                                                               hapticOnError: false)
            guard mine == SessionScope.generation else { return }
            locations = lc
            locationsLoadedAt = Date()
        } catch {
            guard mine == SessionScope.generation else { return }
            if !(error is CancellationError) { locations = nil }
        }
    }
}

// MARK: - Tone

/// One colour table for the comparison: good green, behind amber, level
/// ink — never red (a standing is not an emergency) and never ember
/// (ember is not status).
enum BenchmarkTone {
    static func color(_ tone: String?) -> Color {
        switch tone {
        case "good": return .cavnarGreen
        case "warn": return .cavnarAmber
        default: return .cavnarInk3
        }
    }
}

// MARK: - The card

/// "How you compare" on Labor, Food Cost, Reviews and Marketing
/// (Benchmarking audit 9/24/26, #23 / #18). A composition of existing
/// parts, no new pattern: `.cavnarCard()`, a kicker, the comparison
/// strength as a `ConfidenceMeter` line whose Why? opens the Account-kit
/// sheet with the confidence drawer's rows (`ConfidenceDimensionRow`), one
/// row per metric with its tone dot, and `HomeAskLink` for a metric the
/// restaurant is behind on. Below the minimum it says so and leads with
/// the restaurant's own previous 13 weeks. Draws nothing until the server
/// answers, and nothing for a login that can't see the module.
struct HowYouCompareCard: View {
    /// labor | food_cost | reviews | marketing
    let module: String
    @State private var store = BenchmarkCardStore.shared
    @State private var showingWhy = false
    @State private var showingProfile = false
    @State private var profileCanEdit = true

    var body: some View {
        Group {
            if let card = store.card(for: module), card.hasContent {
                content(card)
            }
        }
        .task { await store.load(module) }
        .sheet(isPresented: $showingProfile) {
            AccountRestaurantProfileSheet(canEdit: profileCanEdit)
        }
    }

    private func content(_ card: BenchmarkCard) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("HOW YOU COMPARE")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            if let who = card.who, let text = who.text {
                HomeMixedText.make(text, size: 15.5, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                if let sub = who.subline {
                    HomeMixedText.make(sub, size: 12.5, weight: 500, color: .cavnarInk3)
                }
            }
            if let s = card.strength { strengthLine(s) }
            if let b = card.belowMinimum, let text = b.text {
                VStack(alignment: .leading, spacing: 3) {
                    Text(text)
                        .font(.cavnarBody(13.5, weight: 500))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    if let why = b.whyNot {
                        Text("Why: " + why)
                            .font(.cavnarBody(12.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let a = b.profileAction {
                        BenchmarkProfileAction(action: a) {
                            profileCanEdit = a.canEdit ?? true
                            showingProfile = true
                        }
                    }
                }
            }
            if card.rows.isEmpty {
                if let empty = card.empty {
                    Text(empty)
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(card.rows.enumerated()), id: \.element.id) { i, row in
                        HowYouCompareRow(row: row)
                        if i < card.rows.count - 1 { AccountRowDivider() }
                    }
                }
                if let n = card.unmeasured, n > 0 {
                    Text("\(n) more not measured yet")
                        .font(.cavnarBody(12.5))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("How you compare")
        .sheet(isPresented: $showingWhy) {
            if let s = card.strength { ComparisonStrengthSheet(strength: s) }
        }
    }

    private func strengthLine(_ s: BenchmarkStrength) -> some View {
        let tone = ConfidenceDisplay.tone(pct: s.pct)
        return HStack(alignment: .firstTextBaseline, spacing: 8) {
            ConfidenceMeter(fraction: ConfidenceDisplay.fraction(s.pct), tone: tone)
                .alignmentGuide(.firstTextBaseline) { dim in dim[.bottom] + 1 }
            (HomeMixedText.make(s.lineLabel ?? "", size: 12.5, weight: 700, color: tone.color)
             + (s.reason.map { HomeMixedText.make(" \u{2014} " + $0, size: 12.5, weight: 500, color: .cavnarInk3) }
                ?? Text(verbatim: "")))
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel("\(s.pct ?? 0) percent comparison strength. \(s.reason ?? "")")
            Button {
                Haptic.light()
                showingWhy = true
            } label: {
                Text("Why?")
                    .font(.cavnarBody(12.5, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .padding(.vertical, 4)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Shows what this comparison rests on")
            Spacer(minLength: 0)
        }
    }
}

/// Why there is no like-for-like group, fixed where it can be (#29): the
/// engine's "We think you're X — is that right?" and a Confirm your profile
/// link that opens the Account profile sheet — or, for a login that may
/// not change it, who can.
struct BenchmarkProfileAction: View {
    let action: BenchmarkBelowMinimum.Action
    let onOpen: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if let s = action.suggestion {
                Text(s)
                    .font(.cavnarBody(12.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if action.canEdit == false {
                Text("The account owner can confirm it in Account.")
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk3)
            } else {
                Button {
                    Haptic.light()
                    onOpen()
                } label: {
                    Text((action.label ?? "Confirm your profile") + " \u{2192}")
                        .font(.cavnarBody(12.5, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .padding(.vertical, 4)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityHint("Opens your restaurant profile in Account")
            }
        }
    }
}

/// One metric's row: its tone dot, "Labor % 35% — worse than your normal",
/// what it is read against, and for a metric the restaurant is behind on,
/// the one action. Compact (Home): the row's own group and strength tag,
/// and Open for a row with nothing to ask.
struct HowYouCompareRow: View {
    let row: BenchmarkRow
    var compact: Bool = false
    var showsTag: Bool = false
    var onOpenModule: ((String) -> Void)? = nil

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Circle().fill(BenchmarkTone.color(row.tone))
                .shadow(color: row.tone == "good" || row.tone == "warn" ? BenchmarkTone.color(row.tone).opacity(0.8) : .clear,
                        radius: 3.5)
                .frame(width: 7, height: 7)
                .alignmentGuide(.firstTextBaseline) { d in d[.bottom] - 1 }
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                HomeMixedText.make(row.headline, size: compact ? 13.5 : 14.5, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                if !compact, let detail = row.detail {
                    HomeMixedText.make(detail, size: 12.5, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if compact, showsTag, let tag = row.tag {
                    HomeMixedText.make(tag, size: 12, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !compact, let note = row.note {
                    Text(note)
                        .font(.cavnarBody(12.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let ask = row.action?.ask {
                    HomeAskLink(question: ask, label: row.action?.label ?? "Ask what to change")
                } else if compact, let module = row.openModule, let onOpenModule {
                    Button {
                        Haptic.light()
                        onOpenModule(module)
                    } label: {
                        Text("Open \u{2192}")
                            .font(.cavnarBody(12.5, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .padding(.vertical, 2)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Open \(row.label)")
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, compact ? 6 : 10)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - The Why? sheet

/// What the comparison strength rests on — peer count, band freshness, the
/// restaurant's own figure and the type match — in the confidence Why?
/// sheet's Account-kit layout and rows. Leads with what the % means: how
/// well supported the comparison is, not how well the restaurant is doing.
struct ComparisonStrengthSheet: View {
    let strength: BenchmarkStrength

    var body: some View {
        let tone = ConfidenceDisplay.tone(pct: strength.pct)
        let rows = strength.displayRows
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    VStack(alignment: .leading, spacing: 10) {
                        AccountKicker(text: "How strong is this comparison")
                        if let meaning = strength.meaning {
                            Text(meaning + ".")
                                .font(.cavnarBody(13.5, weight: 500))
                                .foregroundStyle(Color.cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if let pct = strength.pct {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text("\(pct)%")
                                    .font(.cavnarNumber(40, weight: 600))
                                    .foregroundStyle(tone.color)
                                    .cavnarNumberGlow(tone.color)
                                Text("comparison strength")
                                    .font(.cavnarBody(15.5, weight: 600))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            .accessibilityElement(children: .combine)
                            .accessibilityLabel("\(pct) percent comparison strength")
                        }
                        ConfidenceMeter(fraction: ConfidenceDisplay.fraction(strength.pct), tone: tone, width: nil, height: 8)
                            .frame(maxWidth: .infinity)
                        if let reason = strength.reason {
                            HomeMixedText.make(reason, size: 15, weight: 500, color: .cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    AccountSection(kicker: "What it rests on") {
                        ForEach(Array(rows.enumerated()), id: \.offset) { i, row in
                            ConfidenceDimensionRow(row: row, showsDivider: i < rows.count - 1)
                        }
                    }
                    if let footer = strength.footer {
                        HomeMixedText.make(footer, size: 12.5, weight: 500, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Comparison strength")
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }
}

// MARK: - Home strip

/// Home's compact "How you compare" line under the freshness strip: who the
/// restaurant is compared to, then up to six metrics, what it is behind on
/// first, each with its tone dot and — when behind — the Ask link. Nothing
/// when there is nothing to compare.
struct HomeBenchmarkStrip: View {
    /// Open a row's module (labor | inventory | reviews | marketing) — the
    /// web strip's Open (#33).
    var onOpenModule: ((String) -> Void)? = nil
    @State private var store = BenchmarkCardStore.shared
    @State private var showingProfile = false
    @State private var profileCanEdit = true

    var body: some View {
        Group {
            if let card = store.card(for: "home"), card.ok, !card.rows.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(alignment: .firstTextBaseline, spacing: 5) {
                        HomeMixedText.make(Self.kicker(card), size: 11, weight: 700, color: .cavnarInk3,
                                           numberColor: .cavnarInk2)
                            .tracking(1.1)
                    }
                    VStack(alignment: .leading, spacing: 0) {
                        // Rows from different groups each name their own
                        // group and strength (#26); one group is in the kicker.
                        ForEach(card.rows) { row in
                            HowYouCompareRow(row: row, compact: true, showsTag: card.strength == nil,
                                             onOpenModule: onOpenModule)
                        }
                    }
                    if let b = card.belowMinimum, let text = b.text {
                        Text(text)
                            .font(.cavnarBody(12.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                        if let a = b.profileAction {
                            BenchmarkProfileAction(action: a) {
                                profileCanEdit = a.canEdit ?? true
                                showingProfile = true
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .task { await store.load("home") }
        .sheet(isPresented: $showingProfile) {
            AccountRestaurantProfileSheet(canEdit: profileCanEdit)
        }
    }

    /// "HOW YOU COMPARE · VS YOUR OWN PREVIOUS 13 WEEKS · 72% COMPARISON
    /// STRENGTH" — the web's wording, one wording on both clients (#33).
    static func kicker(_ card: BenchmarkCard) -> String {
        var parts = ["HOW YOU COMPARE"]
        if let who = card.who?.text { parts.append(who.uppercased()) }
        if let pct = card.strength?.pct { parts.append("\(pct)% COMPARISON STRENGTH") }
        return parts.joined(separator: " \u{00B7} ")
    }
}

// MARK: - Location to location

/// The owner's locations side by side (#19), in the locations sheet: per
/// metric, each location's figure, its read against its own normal, and
/// against the owner's other locations — a gap called only when it is wider
/// than both locations' own week-to-week swing, else "in line".
struct LocationComparisonSection: View {
    @State private var store = BenchmarkCardStore.shared

    var body: some View {
        Group {
            if let lc = store.currentLocations, lc.ok {
                if lc.metrics.isEmpty {
                    if let why = lc.whyNot {
                        Text("Locations compared side by side appear once two or more are measured \u{2014} \(why).")
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } else {
                    VStack(alignment: .leading, spacing: 14) {
                        Text("Each against its own normal first, then against your other locations \u{2014} a gap is called only when it is wider than both locations\u{2019} own week-to-week swing.")
                            .font(.cavnarBody(12.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                        ForEach(lc.metrics) { m in
                            AccountSection(kicker: m.label) {
                                ForEach(Array(m.locations.enumerated()), id: \.element.id) { i, l in
                                    locationRow(l)
                                    if i < m.locations.count - 1 { AccountRowDivider() }
                                }
                            }
                        }
                    }
                }
            }
        }
        .task { await store.loadLocations() }
    }

    private func locationRow(_ l: LocationComparison.Location) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Circle().fill(BenchmarkTone.color(l.tone))
                .frame(width: 7, height: 7)
                .alignmentGuide(.firstTextBaseline) { d in d[.bottom] - 1 }
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                HomeMixedText.make(l.name + (l.valueText.map { " \($0)" } ?? "")
                                   + (l.vsGroup.map { " \u{2014} \($0)" } ?? ""),
                                   size: 14, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                HomeMixedText.make(l.detail, size: 12.5, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 9)
        .accessibilityElement(children: .combine)
    }
}
