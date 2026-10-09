import SwiftUI
import Observation

/// Recommendations on the phone (iOS re-audit M17, "Web explains. iPhone
/// decides."): what only the owner can answer — the results that landed
/// and ask "Did you make this change?", the kinds held back ("Keep
/// suggesting it?") and the kinds gone quieter ("Show … again") — and one
/// link to the full record on the web (rec-ROI #13, #20, #35: what was
/// followed by module, what worked best, every recommendation with its
/// measurement and result). The view model still reads the whole record.
///
/// Reached from Account → Recommendations and from Home's "What Cavnar AI
/// has been worth". Built from the Account identity-card kit.
struct RecommendationHistoryView: View {
    /// The kinds Cavnar AI holds back on this restaurant's own record,
    /// each asked "Keep suggesting it?" (M4) — moved here from Home (iOS
    /// readability round, 10/8/26, #96). Empty from Account.
    var kindHolds: [HomeKindHold] = []
    /// Kinds that went quieter after the last four passed unanswered, each
    /// with "Show … again" (home_brief `quieter`) — also moved from Home.
    var quieter: [HomeQuietKind] = []
    /// "Show … again": POST the kind back (HomeFollowThroughViewModel).
    var onRestoreKind: ((String) async -> Bool)? = nil

    @State private var viewModel = RecommendationHistoryViewModel()
    @State private var restoredNote: String?
    @State private var restoredKinds: Set<String> = []

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Recommendations") {
                        Image(systemName: "checklist")
                            .font(.system(size: 24, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(width: 52, height: 52)
                            .background(Color.cavnarEmber.opacity(0.12))
                            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                    } subtitle: {
                        // One sentence (parity audit #6's words, shortened).
                        Text("What Cavnar AI suggested, what you did, and what was measured after.")
                    }

                    // "Web explains. iPhone decides." (iOS re-audit M17): the
                    // phone keeps what only the owner can answer — the
                    // check-ins, the kinds on hold, the quieter kinds. The
                    // record itself (the window, the rates by module, what
                    // worked best and every recommendation) is the web's.
                    checkInSection
                    kindHoldSection
                    quieterSection
                    if viewModel.checkInsDue.isEmpty && kindHolds.isEmpty && quieter.isEmpty && !viewModel.isLoading {
                        Text("Nothing waiting on you here \u{2014} no results to check in on and no kinds on hold.")
                            .cavnarText(.body)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    CavnarWebLinkRow(title: "Your full record",
                                     subtitle: "What you followed by module, what worked best and every recommendation",
                                     path: "recs", actionLabel: "Open on the web")

                    if let error = viewModel.errorMessage {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Recommendations")
            .task { await viewModel.load() }
        }
    }

    // MARK: - Kinds on hold and quieter kinds (from Home, #96)

    @ViewBuilder
    private var kindHoldSection: some View {
        if !kindHolds.isEmpty {
            HomeKindHolds(holds: kindHolds)
        }
    }

    @ViewBuilder
    private var quieterSection: some View {
        let shown = quieter.filter { !restoredKinds.contains($0.kind) }
        if !shown.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                Text("Quieter")
                    .cavnarText(.headline)
                    .accessibilityAddTraits(.isHeader)
                Text("The last four of these went by unanswered, so Cavnar AI suggests them less.")
                    .cavnarText(.body)
                    .fixedSize(horizontal: false, vertical: true)
                ForEach(shown) { q in
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        Text(q.label).cavnarText(.label)
                        if let back = RecMemoryLines.reviewOn(q.reviewOn) {
                            CavnarMixedText(back, role: .secondary)
                        }
                        if let onRestoreKind {
                            Button {
                                Haptic.light()
                                Task {
                                    if await onRestoreKind(q.kind) {
                                        withAnimation {
                                            restoredKinds.insert(q.kind)
                                            restoredNote = "\(q.label) will show again"
                                        }
                                    }
                                }
                            } label: {
                                Text("Show \(q.label.lowercased()) again")
                                    .cavnarText(.label, color: .cavnarEmber2)
                                    .cavnarHitTarget()
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.vertical, CavnarSpace.xxs)
                }
                if let restoredNote {
                    Text(restoredNote).cavnarText(.secondary, color: .cavnarGreen)
                }
            }
            .cavnarCard()
        }
    }

    // MARK: - Check in

    /// Results that landed and wait on "Did you make this change?" — at most
    /// five, each joined to its recommendation in the timeline.
    @ViewBuilder
    private var checkInSection: some View {
        let due = viewModel.checkInsDue
        if !due.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                HomeSectionHeader(kicker: "Check in", title: "Did you make these changes?",
                                  trailing: "\(due.count) result\(due.count == 1 ? "" : "s") landed")
                ForEach(due) { o in
                    RecCheckInCard(outcome: o, surface: "ios") { await viewModel.reloadOutcomes() }
                }
            }
        }
    }
}

// MARK: - View model

@Observable
@MainActor
final class RecommendationHistoryViewModel {
    enum Window: Int, CaseIterable, Hashable {
        case d30 = 30, d90 = 90, d180 = 180
        var label: String { "\(rawValue) days" }
    }

    var window: Window = .d90
    var summary: RecSummary?
    var items: [RecTimelineItem] = []
    var nextBefore: String?
    var outcomesById: [Int: RecOutcome] = [:]
    var caveat: String?
    var isLoading = false
    var isLoadingSummary = false
    var isLoadingMore = false
    var errorMessage: String?
    /// The tracker a "Stop measuring" is in flight for.
    var stopping: Int?

    static let pageSize = 30
    private let client: APIClient

    init(client: APIClient = .shared) { self.client = client }

    func outcome(for item: RecTimelineItem) -> RecOutcome? {
        item.trackerId.flatMap { outcomesById[$0] }
    }

    /// GET /recs/what-worked — "Measured alongside your changes", over 90
    /// days at least (its sentences carry their own window), as the web.
    var whatWorked: WhatWorked?

    /// The check-ins at the top: results that landed with no answer yet,
    /// each joined to its recommendation in the timeline, newest first, up
    /// to five (the web's `recCheckinCandidates`).
    var checkInsDue: [RecOutcome] {
        var seen = Set<Int>()
        var out: [RecOutcome] = []
        for item in items {
            guard let id = item.trackerId, !seen.contains(id), let o = outcomesById[id], RecCheckIn.isDue(o) else { continue }
            seen.insert(id)
            out.append(o)
            if out.count == 5 { break }
        }
        return out
    }

    var checkInIds: Set<Int> { Set(checkInsDue.map(\.id)) }

    func loadWhatWorked() async {
        let days = max(90, window.rawValue)
        let w: WhatWorked? = try? await client.send("/mobile/api/recs/what-worked", query: ["days": "\(days)"],
                                                    hapticOnError: false)
        whatWorked = w
    }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        errorMessage = nil
        async let s: Void = loadSummary()
        async let w: Void = loadWhatWorked()
        async let o: Void = reloadOutcomes()
        async let t: RecTimelinePage? = try? client.send("/mobile/api/recs/timeline",
                                                         query: ["limit": "\(Self.pageSize)"], hapticOnError: false)
        _ = await (s, w, o)
        if let page = await t, page.ok {
            items = page.items
            nextBefore = page.nextBefore
            await loadLinkedOutcomes()
        } else if items.isEmpty {
            errorMessage = "The timeline didn\u{2019}t load \u{2014} close this and open it again to retry."
        }
    }

    func loadSummary() async {
        isLoadingSummary = true
        defer { isLoadingSummary = false }
        let asked = window
        let r: RecSummary? = try? await client.send("/mobile/api/recs/summary",
                                                    query: ["days": "\(asked.rawValue)"], hapticOnError: false)
        // A quick second tap on the segments: keep the answer for the one showing.
        guard asked == window else { return }
        if let r, r.ok { summary = r }
    }

    func loadMore() async {
        guard let cursor = nextBefore, !isLoadingMore else { return }
        isLoadingMore = true
        defer { isLoadingMore = false }
        let page: RecTimelinePage? = try? await client.send(
            "/mobile/api/recs/timeline", query: ["limit": "\(Self.pageSize)", "before": cursor], hapticOnError: false)
        guard let page, page.ok else { return }
        let seen = Set(items.map(\.id))
        items += page.items.filter { !seen.contains($0.id) }
        nextBefore = page.nextBefore
        await loadLinkedOutcomes()
    }

    /// The results the timeline's items link to that the newest-first list
    /// did not carry (an older recommendation's tracker): asked for by id.
    func loadLinkedOutcomes() async {
        let missing = Array(Set(items.compactMap(\.trackerId)).subtracting(outcomesById.keys)).sorted()
        guard !missing.isEmpty else { return }
        for chunk in stride(from: 0, to: missing.count, by: 100).map({ Array(missing[$0..<min($0 + 100, missing.count)]) }) {
            guard let r: RecOutcomesResponse = try? await client.send(
                "/mobile/api/outcomes", query: ["ids": chunk.map(String.init).joined(separator: ",")],
                hapticOnError: false), r.ok else { continue }
            for o in r.outcomes { outcomesById[o.id] = o }
        }
    }

    func reloadOutcomes() async {
        guard let r: RecOutcomesResponse = try? await client.send("/mobile/api/outcomes", hapticOnError: false),
              r.ok else { return }
        outcomesById = Dictionary(r.outcomes.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        caveat = r.caveat
        await loadLinkedOutcomes()
    }

    @discardableResult
    func stopMeasuring(_ outcome: RecOutcome) async -> Bool {
        guard stopping == nil else { return false }
        stopping = outcome.id
        defer { stopping = nil }
        do {
            let r = try await client.abandonOutcome(id: outcome.id)
            guard r.ok else {
                errorMessage = r.error ?? "Couldn\u{2019}t stop that."
                return false
            }
            Haptic.success()
            await reloadOutcomes()
            return true
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return false
        } catch {
            return false
        }
    }
}
