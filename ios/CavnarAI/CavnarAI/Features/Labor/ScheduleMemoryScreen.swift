import SwiftUI

// What the schedule has learned (GET/POST labor/schedule-memory, H2-1) and
// the servers' measured ratings (GET/POST labor/ratings/suggested, H2-3) —
// schedule audit 10/3/26 L-29, L-23. Account-kit sheets; every line is the
// server's, every figure a computed %, "—" below its sample floor.

@Observable
@MainActor
final class ScheduleMemoryViewModel {
    var view: ScheduleMemoryView?
    var isLoading = false
    var error: String?
    var busyKey: String?
    /// The server's answer per fact: the rule's words, or its refusal.
    var notes: [String: String] = [:]

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    func load() async {
        isLoading = view == nil
        defer { isLoading = false }
        do {
            let v: ScheduleMemoryView = try await client.send("/mobile/api/labor/schedule-memory", hapticOnError: false)
            view = v
            error = nil
        } catch let e as APIClient.APIError {
            if view == nil { error = e.message }
        } catch is CancellationError {
        } catch {
            if view == nil { self.error = "Couldn\u{2019}t read what the schedule has learned." }
        }
    }

    private struct AnswerBody: Encodable { let key: String; let action: String }
    private struct AnswerResponse: Decodable {
        let ok: Bool
        var error: String? = nil
        var rule: AnyCodableValue? = nil
    }

    /// keep | let_go | rule. A refusal (400) shows the server's words.
    func answer(_ item: ScheduleMemoryItem, action: String) async {
        busyKey = item.key
        defer { busyKey = nil }
        do {
            let r: AnswerResponse = try await client.send(
                "/mobile/api/labor/schedule-memory", method: .post,
                body: AnswerBody(key: item.key, action: action), hapticOnError: false)
            if r.ok {
                Haptic.success()
                let ruleWords = r.rule?.objectValue?["text"]?.stringValue ?? r.rule?.stringValue
                notes[item.key] = action == "rule" ? (ruleWords.map { "Now a rule: \($0)" } ?? "Now a rule")
                    : action == "keep" ? "Kept" : "Let go"
                await load()
            } else {
                notes[item.key] = r.error ?? "Couldn\u{2019}t do that."
            }
        } catch let e as APIClient.APIError {
            notes[item.key] = e.message
        } catch {
            notes[item.key] = "Couldn\u{2019}t do that."
        }
    }

    /// The facts grouped by their class, in the order they first appear.
    var groups: [(label: String, items: [ScheduleMemoryItem])] {
        var order: [String] = []
        var by: [String: [ScheduleMemoryItem]] = [:]
        for item in view?.items ?? [] {
            let label = item.classLabel ?? "Other"
            if by[label] == nil { order.append(label) }
            by[label, default: []].append(item)
        }
        return order.map { ($0, by[$0] ?? []) }
    }
}

struct ScheduleMemoryScreen: View {
    @State private var viewModel = ScheduleMemoryViewModel()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("Habits, teams and patterns the draft keeps, each with how sure Cavnar AI is and when a hand last confirmed it. Keep one, let it go, or make it a rule every draft must keep.")
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    if viewModel.isLoading {
                        CavnarSkeletonLines(widths: [1.0, 0.8, 0.6, 0.9, 0.5])
                    } else if let error = viewModel.error {
                        Text(error).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarRed)
                        Button("Try again") { Task { await viewModel.load() } }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                    } else if viewModel.groups.isEmpty {
                        Text("Nothing learned yet. As drafts are edited and published, what your managers keep doing shows up here.")
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        ForEach(viewModel.groups, id: \.label) { group in
                            AccountSection(kicker: group.label) {
                                VStack(alignment: .leading, spacing: 0) {
                                    ForEach(group.items) { item in
                                        row(item)
                                        AccountRowDivider()
                                    }
                                }
                                .accountCard()
                            }
                        }
                    }
                    if let at = viewModel.view?.consolidatedAt {
                        HomeMixedText.make("Updated \(at)", size: 12.5, color: .cavnarInk3)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("What the schedule has learned")
            .task { await viewModel.load() }
        }
    }

    private func statusTone(_ status: String?) -> Color {
        switch status {
        case "rule": return .cavnarGreen
        case "active": return .cavnarEmber2
        case "retest", "candidate": return .cavnarAmber
        default: return .cavnarInk3
        }
    }

    private func row(_ item: ScheduleMemoryItem) -> some View {
        let retired = item.status == "retired"
        let canAnswer = viewModel.view?.canAnswer == true
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                HomeMixedText.make(item.text ?? item.key, size: 15, weight: 600, color: retired ? .cavnarInk3 : .cavnarInk)
                    .strikethrough(retired, color: Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 6)
                if let pct = item.confidencePct {
                    Text(pct).font(.cavnarNumber(14, weight: 700)).foregroundStyle(Color.cavnarInk2)
                }
            }
            HStack(spacing: 6) {
                if let label = item.statusLabel {
                    ScheduleRowTag(text: label, tone: statusTone(item.status))
                }
                if item.heldInCode { ScheduleRowTag(text: "Held by Cavnar AI\u{2019}s checks", tone: .cavnarGreen) }
                if let bound = item.boundWords { ScheduleRowTag(text: bound, tone: .cavnarInk2) }
            }
            HomeMixedText.make([item.evidence.map { "\($0) times" },
                                item.lastConfirmedByHand.map { "last confirmed by hand \($0)" },
                                retired ? item.retiredWords : nil].compactMap { $0 }.joined(separator: " \u{00B7} "),
                               size: 12.5, color: .cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            if let note = viewModel.notes[item.key] {
                HomeMixedText.make(note, size: 13, weight: 600,
                                   color: ["Kept", "Let go"].contains(note) || note.hasPrefix("Now a rule") ? .cavnarGreen : .cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if canAnswer, item.canKeep || item.canLetGo || item.canBeRule {
                HStack(spacing: 16) {
                    if item.canKeep { action("Keep", item, "keep") }
                    if item.canLetGo { action("Let it go", item, "let_go") }
                    if item.canBeRule { action("Make it a rule", item, "rule") }
                }
            }
        }
        .padding(.vertical, 10)
    }

    private func action(_ label: String, _ item: ScheduleMemoryItem, _ key: String) -> some View {
        Button {
            Task { await viewModel.answer(item, action: key) }
        } label: {
            Text(viewModel.busyKey == item.key ? "Saving" : label)
                .font(.cavnarBody(13.5, weight: 700))
                .foregroundStyle(key == "let_go" ? Color.cavnarInk3 : Color.cavnarEmber2)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(viewModel.busyKey != nil)
    }
}

// MARK: - Measured ratings (H2-3)

@Observable
@MainActor
final class MeasuredRatingsViewModel {
    var data: SuggestedRatings?
    var isLoading = false
    var error: String?
    var busy: String?
    var confirmed: [String: Int] = [:]
    var rowError: [String: String] = [:]

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    func load() async {
        isLoading = data == nil
        defer { isLoading = false }
        do {
            data = try await client.send("/mobile/api/labor/ratings/suggested", hapticOnError: false)
            error = nil
        } catch let e as APIClient.APIError {
            error = e.message
        } catch is CancellationError {
        } catch {
            self.error = "Couldn\u{2019}t read the measured ratings."
        }
    }

    private struct ConfirmBody: Encodable { let name: String; let score: Int? }
    private struct ConfirmResponse: Decodable { let ok: Bool; var score: Int? = nil; var error: String? = nil }

    /// Confirm the suggestion (score nil) or set another score.
    func confirm(_ s: SuggestedRating, score: Int? = nil) async {
        busy = s.name
        defer { busy = nil }
        do {
            let r: ConfirmResponse = try await client.send(
                "/mobile/api/labor/ratings/suggested", method: .post,
                body: ConfirmBody(name: s.name, score: score), hapticOnError: false)
            if r.ok {
                confirmed[s.name] = r.score ?? score ?? s.suggested
                rowError[s.name] = nil
                Haptic.success()
            } else {
                rowError[s.name] = r.error ?? "Couldn\u{2019}t save that."
            }
        } catch let e as APIClient.APIError {
            rowError[s.name] = e.message
        } catch {
            rowError[s.name] = "Couldn\u{2019}t save that."
        }
    }
}

struct MeasuredRatingsScreen: View {
    @State private var viewModel = MeasuredRatingsViewModel()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if viewModel.isLoading {
                        CavnarSkeletonLines(widths: [1.0, 0.8, 0.6, 0.9])
                    } else if let error = viewModel.error {
                        Text(error).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    } else if let data = viewModel.data {
                        if let note = data.note {
                            Text(note).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if data.servers.isEmpty {
                            HomeMixedText.make(data.reason ?? "No server has enough tickets yet\(data.minTickets.map { " (\($0) are needed)" } ?? "").",
                                               size: 14.5, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        } else {
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(data.servers) { s in
                                    row(s)
                                    AccountRowDivider()
                                }
                            }
                            .accountCard()
                        }
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Measured ratings")
            .task { await viewModel.load() }
        }
    }

    private func row(_ s: SuggestedRating) -> some View {
        let done = viewModel.confirmed[s.name]
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(s.name).font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk)
                Spacer()
                HomeMixedText.make("Now \(s.current.map(String.init) ?? "—")", size: 13, weight: 600, color: .cavnarInk3)
            }
            if let suggested = s.suggested {
                HomeMixedText.make("Suggested \(suggested)" + (s.reason.map { " \u{2014} \($0)" } ?? ""),
                                   size: 13.5, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let done {
                HomeMixedText.make("Rated \(done) \u{2014} it\u{2019}s your Operational Score now", size: 13, weight: 600,
                                   color: .cavnarGreen)
            } else {
                HStack(spacing: 6) {
                    if s.differs, s.suggested != nil {
                        Button("Confirm") { Task { await viewModel.confirm(s) } }
                            .buttonStyle(RecAnswerPillStyle(selected: false))
                    }
                    ForEach(1...5, id: \.self) { v in
                        Button("\(v)") { Task { await viewModel.confirm(s, score: v) } }
                            .font(.cavnarNumber(14, weight: 700))
                            .foregroundStyle(Color.cavnarInk2)
                            .frame(width: 34, height: 34)
                            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.cavnarPaper2))
                            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                                .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                            .buttonStyle(.plain)
                            .accessibilityLabel("Rate \(s.name) \(v)")
                    }
                }
                .disabled(viewModel.busy != nil)
            }
            if let e = viewModel.rowError[s.name] {
                Text(e).font(.cavnarBody(13)).foregroundStyle(Color.cavnarRed)
            }
        }
        .padding(.vertical, 10)
    }
}
