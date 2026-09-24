import SwiftUI
import Observation

/// Recommendations — the owner's own record (rec-ROI #13, #20, #35): what
/// they followed by module over 30 / 90 / 180 days (a rate only once
/// enough have settled, ignored counted against it), the subject that has
/// worked best for this restaurant, then every recommendation they were
/// shown, newest first, with what they did, why, what is being measured
/// until when (with the partial reading so far), and what the result was —
/// its attribution in plain words, what else changed those weeks, and the
/// re-check. A result that landed without a check-in asks for one here; a
/// measurement still running can be stopped.
///
/// Reached from Account → Recommendations and from Home's "What Cavnar AI
/// has been worth". Built from the Account identity-card kit.
struct RecommendationHistoryView: View {
    @State private var viewModel = RecommendationHistoryViewModel()
    @State private var confirmingStop: RecOutcome?
    @State private var showingStopConfirm = false

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
                        Text("What you followed, and what it did.")
                    }

                    CavnarSegmentedControl(selection: $viewModel.window,
                                           options: RecommendationHistoryViewModel.Window.allCases) { $0.label }

                    followedSection
                    mostEffectiveSection
                    timelineSection

                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Recommendations")
            .task { await viewModel.load() }
            .onChange(of: viewModel.window) { _, _ in
                Task { await viewModel.loadSummary() }
            }
            .confirmationDialog("Stop measuring this?", isPresented: $showingStopConfirm,
                                titleVisibility: .visible, presenting: confirmingStop) { outcome in
                Button("Stop measuring", role: .destructive) {
                    Task { await viewModel.stopMeasuring(outcome) }
                }
                Button("Keep measuring", role: .cancel) {}
            } message: { _ in
                Text("For a change you reversed, or one that no longer applies. Nothing from it is counted.")
            }
        }
    }

    // MARK: - What you followed

    private var followedSection: some View {
        AccountSection(kicker: "What you followed") {
            if viewModel.summary == nil && viewModel.isLoadingSummary {
                CavnarWorkingLine().padding(.vertical, 12)
            } else if let summary = viewModel.summary {
                let modules = summary.modulesInOrder
                if modules.isEmpty {
                    emptyLine("Nothing was recommended in the last \(viewModel.window.rawValue) days.")
                } else {
                    ForEach(Array(modules.enumerated()), id: \.element.key) { index, entry in
                        moduleRow(entry.key, entry.stats, minimum: summary.minSettled,
                                  showsDivider: index < modules.count - 1)
                    }
                    HomeMixedText.make(
                        "Ignored means it expired unanswered after 14 days \u{2014} it counts against the rate. "
                            + "A rate shows once \(summary.minSettled ?? 10) are settled.",
                        size: 13, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 8)
                        .padding(.bottom, 6)
                }
            } else {
                emptyLine("Your record didn\u{2019}t load.")
            }
        }
    }

    private func moduleRow(_ key: String, _ m: RecSummary.Module, minimum: Int?, showsDivider: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline) {
                    Text(RecSummaryFormat.moduleLabel(key))
                        .font(.cavnarBody(16, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    Spacer(minLength: 8)
                    Text(RecSummaryFormat.rate(m))
                        .font(m.enough ? .cavnarNumber(17, weight: 600) : .cavnarBody(13.5, weight: 600))
                        .foregroundStyle(m.enough ? Color.cavnarInk : Color.cavnarInk3)
                }
                HomeMixedText.make(RecSummaryFormat.counts(m), size: 13.5, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                if let range = RecSummaryFormat.range(m) {
                    HomeMixedText.make(range, size: 12.5, weight: 500, color: .cavnarInk3)
                } else if let why = RecSummaryFormat.notEnoughDetail(m, minimum: minimum) {
                    HomeMixedText.make(why, size: 12.5, weight: 500, color: .cavnarInk3)
                }
            }
            .padding(.vertical, 10)
            .accessibilityElement(children: .combine)
            if showsDivider { AccountRowDivider() }
        }
    }

    // MARK: - Most effective

    private var mostEffectiveSection: some View {
        AccountSection(kicker: "Most effective for you") {
            if let e = viewModel.summary?.mostEffective, let line = RecSummaryFormat.mostEffectiveLine(e) {
                HStack(alignment: .top, spacing: 12) {
                    Circle().fill(Color.cavnarGreen).frame(width: 8, height: 8).padding(.top, 7)
                    HomeMixedText.make(line, size: 15, weight: 600, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .padding(.vertical, 10)
            } else if viewModel.summary != nil {
                emptyLine("Not enough measured results yet \u{2014} a subject needs "
                          + "\(viewModel.summary?.minMeasured ?? 5) before it can be called effective.")
            } else {
                emptyLine("\u{2014}")
            }
        }
    }

    // MARK: - Timeline

    private var timelineSection: some View {
        AccountSection(kicker: "Timeline") {
            if viewModel.items.isEmpty {
                if viewModel.isLoading {
                    CavnarWorkingLine().padding(.vertical, 12)
                } else {
                    emptyLine("Nothing yet. Every recommendation Cavnar AI shows you lands here, with what you did about it.")
                }
            } else {
                ForEach(Array(viewModel.items.enumerated()), id: \.element.id) { index, item in
                    RecTimelineRow(
                        item: item,
                        outcome: viewModel.outcome(for: item),
                        stopping: viewModel.stopping,
                        onStop: { outcome in
                            confirmingStop = outcome
                            showingStopConfirm = true
                        },
                        onCheckedIn: { await viewModel.reloadOutcomes() }
                    )
                    if index < viewModel.items.count - 1 { AccountRowDivider() }
                }
                if viewModel.nextBefore != nil {
                    AccountRowDivider()
                    Button {
                        Haptic.light()
                        Task { await viewModel.loadMore() }
                    } label: {
                        Group {
                            if viewModel.isLoadingMore {
                                CavnarShimmerText(text: "Loading older ones", color: Color.cavnarEmber2)
                            } else {
                                Text("Show older ones")
                            }
                        }
                        .font(.cavnarBody(14, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 12)
                    }
                    .buttonStyle(.plain)
                    .disabled(viewModel.isLoadingMore)
                }
                if let caveat = viewModel.caveat {
                    CavnarCaveat(title: "Before and after, not proof", detail: caveat)
                        .padding(.vertical, 10)
                }
            }
        }
    }

    private func emptyLine(_ text: String) -> some View {
        HomeMixedText.make(text, size: 14, weight: 500, color: .cavnarInk3)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.vertical, 10)
    }
}

// MARK: - One recommendation in the timeline

private struct RecTimelineRow: View {
    let item: RecTimelineItem
    let outcome: RecOutcome?
    let stopping: Int?
    let onStop: (RecOutcome) -> Void
    let onCheckedIn: () async -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HomeMixedText.make(item.title, size: 15, weight: 600, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            HomeMixedText.make(item.metaLine(), size: 12.5, weight: 500, color: .cavnarInk3)
            AccountFlowLayout(spacing: 6) {
                AccountChip(text: item.answerChip(), muted: !item.wasTaken)
                if outcome?.validated == true {
                    AccountPill(text: "Validated", on: true)
                }
            }
            if let why = item.reasonLine {
                HomeMixedText.make("Why: " + why, size: 13, weight: 500, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let made = item.madeTheChangeLine() {
                HomeMixedText.make(made, size: 13, weight: 500, color: .cavnarInk3)
            }
            if let outcome {
                outcomeBlock(outcome)
            }
        }
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func outcomeBlock(_ o: RecOutcome) -> some View {
        if o.isTracking {
            if let line = o.measuringLine { RecTrackerLine(text: line) }
            if let interim = o.interimLine {
                HStack(alignment: .firstTextBaseline, spacing: 7) {
                    Text("PARTIAL")
                        .font(.cavnarBody(10, weight: 700))
                        .tracking(0.9)
                        .foregroundStyle(Color.cavnarAmber)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Color.cavnarAmberBg, in: Capsule())
                    HomeMixedText.make(interim, size: 13, weight: 500, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
            }
            Button {
                Haptic.light()
                onStop(o)
            } label: {
                if stopping == o.id {
                    CavnarShimmerText(text: "Stopping", color: Color.cavnarRed)
                } else {
                    Text("Stop measuring")
                        .font(.cavnarBody(13, weight: 600))
                        .foregroundStyle(Color.cavnarRed)
                }
            }
            .buttonStyle(.plain)
            .disabled(stopping != nil)
            .padding(.top, 2)
        } else if o.isEvaluated {
            if let result = o.resultLine ?? o.summary {
                HStack(alignment: .top, spacing: 10) {
                    Circle().fill(o.tone).frame(width: 7, height: 7).padding(.top, 6)
                    HomeMixedText.make(result, size: 13.5, weight: 600, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let label = o.attributionLabel, !label.isEmpty {
                HomeMixedText.make(label, size: 13, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let other = o.otherChangesLine {
                HomeMixedText.make(other, size: 13, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let recheck = o.recheckLine {
                HomeMixedText.make(recheck, size: 13, weight: 500,
                                   color: o.recheckVerdict == "held" ? .cavnarGreen : .cavnarInk3)
            }
            if RecCheckIn.isDue(o) {
                RecCheckInCard(outcome: o, surface: "ios", onAnswered: onCheckedIn)
                    .padding(.top, 4)
            }
        } else if o.status == "abandoned" {
            HomeMixedText.make("Stopped measuring", size: 13, weight: 500, color: .cavnarInk3)
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

    func load() async {
        isLoading = true
        defer { isLoading = false }
        errorMessage = nil
        async let s: Void = loadSummary()
        async let o: Void = reloadOutcomes()
        async let t: RecTimelinePage? = try? client.send("/mobile/api/recs/timeline",
                                                         query: ["limit": "\(Self.pageSize)"], hapticOnError: false)
        _ = await (s, o)
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
