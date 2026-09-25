import SwiftUI
import Observation

// MARK: - Store

/// The one read of GET /mobile/api/data-health the app keeps: the Data
/// health sheet forces a fresh one each time it opens; a module's badge
/// reuses whatever landed in the last two minutes rather than asking again
/// on every screen. Dropped when the session (user or location) changes,
/// so one restaurant's sources are never shown under another.
@Observable
@MainActor
final class DataHealthStore {
    static let shared = DataHealthStore()

    private(set) var snapshot: DataHealthSnapshot?
    private(set) var loadedAt: Date?
    private(set) var isLoading = false
    private(set) var errorMessage: String?
    private(set) var isSyncing = false
    /// The server's own sentence after Sync now ("Syncing now — this
    /// usually takes under a minute.", or that one is already running).
    private(set) var syncMessage: String?
    private(set) var syncFailed = false

    @ObservationIgnored private var generation = SessionScope.generation
    @ObservationIgnored private let client: APIClient
    /// How long a badge may reuse a read.
    static let reuseFor: TimeInterval = 120

    init(client: APIClient = .shared) { self.client = client }

    private func adoptCurrentSession() {
        guard generation != SessionScope.generation else { return }
        generation = SessionScope.generation
        snapshot = nil; loadedAt = nil; errorMessage = nil
        syncMessage = nil; syncFailed = false
    }

    func load(force: Bool = false) async {
        adoptCurrentSession()
        if !force, let at = loadedAt, Date().timeIntervalSince(at) < Self.reuseFor { return }
        if isLoading && !force { return }
        let mine = generation
        isLoading = true
        defer { isLoading = false }
        do {
            let fresh = try await client.dataHealth()
            guard mine == SessionScope.generation else { return }
            if fresh.ok {
                snapshot = fresh
                loadedAt = Date()
                errorMessage = nil
            } else {
                errorMessage = fresh.error ?? "Data health could not be read right now."
            }
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            if mine == SessionScope.generation { errorMessage = error.message }
        } catch {
            if mine == SessionScope.generation { errorMessage = "Data health could not be read right now." }
        }
    }

    /// One "Sync now", then the sheet reads again. The server deduplicates:
    /// a second tap while one runs answers with its own sentence.
    func syncNow(source: String = "pos") async {
        guard !isSyncing else { return }
        isSyncing = true
        syncMessage = nil
        syncFailed = false
        defer { isSyncing = false }
        do {
            let r = try await client.syncDataSource(source)
            if r.ok {
                syncMessage = r.message ?? (r.alreadySyncing
                    ? "A sync is already running \u{2014} this updates when it finishes."
                    : "Syncing now.")
            } else {
                syncMessage = r.error ?? "The sync couldn\u{2019}t start."
                syncFailed = true
            }
        } catch is CancellationError {
            return
        } catch let error as APIClient.APIError {
            syncMessage = error.message
            syncFailed = true
        } catch {
            syncMessage = "The sync couldn\u{2019}t start."
            syncFailed = true
        }
        await load(force: true)
    }

    /// The one line a module screen carries: its weakest source, as the
    /// server wrote it, with that source's health %. Nil when the payload
    /// has not landed or names no source for the module.
    func badge(for module: String) -> DataHealthBadgeLine? {
        guard let s = snapshot?.sources(forModule: module).first else { return nil }
        let pct = s.displayPct.map { " \u{00B7} \($0)% health" } ?? ""
        return DataHealthBadgeLine(text: s.displayLine + pct, tone: s.tone)
    }
}

struct DataHealthBadgeLine: Equatable {
    let text: String
    let tone: String?
}

// MARK: - Tone

/// One colour table for everything data health draws: a source's dot is
/// ok green, warn or bad amber (a stale source is a warning, not an
/// emergency), off ink3. The overall figure: 80% and up green, below it
/// amber, nothing measured ink3. Never ember — ember is not status.
enum DataHealthTone {
    static func dot(_ tone: String?) -> Color {
        switch tone {
        case "ok": return .cavnarGreen
        case "warn", "bad": return .cavnarAmber
        default: return .cavnarInk3
        }
    }

    static func overall(_ pct: Int?) -> Color {
        guard let pct else { return .cavnarInk3 }
        return pct >= 80 ? .cavnarGreen : .cavnarAmber
    }

    static func meter(_ pct: Int?) -> ConfidenceDisplay.Tone {
        (pct ?? 0) >= 80 ? .good : .warn
    }
}

// MARK: - The sheet

/// "Data health" — opened from Home's freshness strip. The Account
/// identity-card kit: the overall % as the hero, then every connected
/// source's line with its dot, reliability and when the next sync runs;
/// what isn't connected and the one step that connects it; what current
/// data would do to each module's recommendations; and Sync now when a
/// source can be pulled on demand.
struct DataHealthSheet: View {
    /// Home's summary, shown in the hero while the full read loads.
    var summary: HomeDataHealth? = nil
    @State private var store = DataHealthStore.shared

    private var overall: DataHealthOverall? { store.snapshot?.overall ?? summary?.overall }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    hero
                    if let snap = store.snapshot {
                        content(snap)
                    } else if let error = store.errorMessage, !store.isLoading {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                                .fixedSize(horizontal: false, vertical: true)
                            Button("Try again") { Task { await store.load(force: true) } }
                                .font(.cavnarBody(14, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                                .buttonStyle(.plain)
                        }
                    } else {
                        AccountSection(kicker: "Sources") {
                            CavnarSkeletonLines(widths: [1.0, 0.8, 0.9, 0.6])
                                .padding(.vertical, 12)
                        }
                        .accessibilityLabel("Loading data health")
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Data health")
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .task { await store.load(force: true) }
    }

    // MARK: Hero

    private var hero: some View {
        let o = overall
        let tone = DataHealthTone.overall(o?.pct)
        return VStack(alignment: .leading, spacing: 10) {
            AccountKicker(text: "How current your data is")
            if let pct = o?.pct {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("\(pct)%")
                        .font(.cavnarNumber(40, weight: 600))
                        .foregroundStyle(tone)
                        .cavnarNumberGlow(tone)
                    Text("data health")
                        .font(.cavnarBody(15.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(pct) percent data health")
                ConfidenceMeter(fraction: Double(pct) / 100, tone: DataHealthTone.meter(pct), width: nil, height: 8)
                    .frame(maxWidth: .infinity)
            } else if o?.isPending == true {
                Text("Waiting for the first sync")
                    .font(.cavnarHeadline(22))
                    .foregroundStyle(Color.cavnarInk2)
            } else if let label = o?.label {
                HomeMixedText.make(label, size: 22, weight: 600, color: .cavnarInk2)
            } else if store.snapshot == nil && store.errorMessage == nil {
                CavnarSkeletonBar(height: 30, widthFraction: 0.4)
            }
            if let reason = o?.reason {
                HomeMixedText.make(reason, size: 15, weight: 500, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let worst = store.snapshot?.worstLine ?? summary?.worstLine {
                HomeMixedText.make(worst, size: 13.5, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: Sections

    @ViewBuilder
    private func content(_ snap: DataHealthSnapshot) -> some View {
        if !snap.sources.isEmpty {
            AccountSection(kicker: "Sources") {
                ForEach(Array(snap.sources.enumerated()), id: \.element.id) { i, s in
                    sourceRow(s, showsDivider: i < snap.sources.count - 1)
                }
            }
        }
        if snap.canSyncNow {
            syncNow
        }
        if !snap.notConnected.isEmpty {
            AccountSection(kicker: "Not connected") {
                ForEach(Array(snap.notConnected.enumerated()), id: \.element.id) { i, n in
                    notConnectedRow(n, showsDivider: i < snap.notConnected.count - 1)
                }
            }
        }
        let impacts = snap.impactLines
        if !impacts.isEmpty {
            AccountSection(kicker: "What it does to your recommendations") {
                ForEach(Array(impacts.enumerated()), id: \.offset) { i, line in
                    VStack(alignment: .leading, spacing: 0) {
                        HomeMixedText.make(line, size: 14.5, weight: 500, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.vertical, 11)
                        if i < impacts.count - 1 { AccountRowDivider() }
                    }
                }
            }
        }
        if snap.sources.isEmpty && snap.notConnected.isEmpty {
            Text("No sources are connected yet. Connect your POS or Google in Account \u{2192} Connections and this fills in.")
                .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func sourceRow(_ s: DataHealthSource, showsDivider: Bool) -> some View {
        let tone = DataHealthTone.dot(s.tone)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Circle().fill(tone)
                    .shadow(color: s.tone == "off" || s.tone == nil ? .clear : tone.opacity(0.8), radius: 3.5)
                    .frame(width: 7, height: 7)
                    .alignmentGuide(.firstTextBaseline) { d in d[.bottom] - 1 }
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    HomeMixedText.make(s.displayLine, size: 14.5, weight: 600, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    if let basis = s.reliability?.basis {
                        HomeMixedText.make(basis, size: 12.5, weight: 500, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let expected = s.expectedLine {
                        HomeMixedText.make(expected, size: 12.5, weight: 500, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let error = s.error {
                        HomeMixedText.make(error, size: 12.5, weight: 500, color: .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 6)
                if let pct = s.displayPct {
                    Text("\(pct)%")
                        .font(.cavnarNumber(15, weight: 600))
                        .foregroundStyle(tone == .cavnarInk3 ? Color.cavnarInk3 : Color.cavnarInk2)
                }
            }
            .padding(.vertical, 11)
            if showsDivider { AccountRowDivider() }
        }
        .accessibilityElement(children: .combine)
    }

    private func notConnectedRow(_ n: DataHealthNotConnected, showsDivider: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 3) {
                Text(n.label ?? n.key)
                    .font(.cavnarBody(14.5, weight: 700))
                    .foregroundStyle(Color.cavnarInk2)
                if let next = n.next {
                    HomeMixedText.make(next, size: 13, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 11)
            if showsDivider { AccountRowDivider() }
        }
        .accessibilityElement(children: .combine)
    }

    private var syncNow: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                Task { await store.syncNow() }
            } label: {
                Group {
                    if store.isSyncing {
                        CavnarShimmerText(text: "Syncing")
                    } else {
                        Text("Sync now")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: store.isSyncing))
            .disabled(store.isSyncing)
            if store.isSyncing {
                CavnarShimmerLine()
                    .accessibilityHidden(true)
            }
            if let message = store.syncMessage {
                HomeMixedText.make(message, size: 13.5, weight: 500,
                                   color: store.syncFailed ? .cavnarRed : .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

// MARK: - Status caption

/// A server status line as a small caption: its tone dot and the sentence,
/// verbatim — ink3 when fine, amber when the tone is warn or bad. The
/// Reviews inbox's fetch line and Marketing's "Metrics synced 9/21/26".
struct ServerStatusCaption: View {
    let status: ServerStatusLine

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Circle().fill(DataHealthTone.dot(status.tone))
                .frame(width: 6, height: 6)
                .accessibilityHidden(true)
            HomeMixedText.make(status.line, size: 12.5, weight: 500,
                               color: status.isWarning ? .cavnarAmber : .cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Module badge

/// One line on a module screen that already shows its own freshness —
/// the weakest source behind that module, in the server's words, with its
/// dot and health %. Tap for the Data health sheet. Reuses the store's
/// last read; draws nothing until one exists or when the module reads no
/// connected source.
struct DataHealthModuleBadge: View {
    let module: String
    @State private var store = DataHealthStore.shared
    @State private var showingSheet = false

    var body: some View {
        Group {
            if let badge = store.badge(for: module) {
                Button {
                    Haptic.light()
                    showingSheet = true
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Circle().fill(DataHealthTone.dot(badge.tone))
                            .frame(width: 6, height: 6)
                            .alignmentGuide(.firstTextBaseline) { d in d[.bottom] - 1 }
                        HomeMixedText.make(badge.text, size: 12.5, weight: 500, color: .cavnarInk3)
                            .lineLimit(2)
                            .multilineTextAlignment(.leading)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityHint("Opens data health")
            }
        }
        .task { await store.load() }
        .sheet(isPresented: $showingSheet) { DataHealthSheet() }
    }
}
