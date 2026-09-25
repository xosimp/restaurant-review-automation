import SwiftUI
import Observation

/// A stored Ask proposal, opened again from where it was left — the command
/// sheet's "Waiting on you" row for it (F3-2). Read back with
/// GET /mobile/api/ask-cavnar/proposals/<id> (command_center.reopen: no
/// model call) and confirmed through Ask's own ProposalCard, so the proposed
/// row is the one settled. A proposal that has since been confirmed or
/// dismissed says how it ended instead of offering Confirm again.
///
/// It is NOT PendingActionSheet: `action/<id>` there is a delayed_actions id,
/// a different id space, and opening a proposal through it answered "That
/// already went out" — or showed an unrelated queued send with the same
/// number.
struct ProposalReopenResponse: Decodable {
    struct Settled: Decodable {
        let outcome: String?
        let at: String?
    }
    let ok: Bool
    let proposal: AskProposal?
    let settled: Settled?
    let summary: String?
    let error: String?

    /// "Confirmed 9/24/26" / "Dismissed" — what happened to a settled one.
    var settledLine: String? {
        guard let settled else { return nil }
        let word: String
        switch settled.outcome {
        case "confirmed", "accepted", "done": word = "Confirmed"
        case "dismissed", "rejected", "not_now": word = "Dismissed"
        case let other?: word = other.replacingOccurrences(of: "_", with: " ").capitalized
        case nil: word = "Settled"
        }
        let when = settled.at.map { " " + CavnarDate.mdy($0) } ?? ""
        return word + when
    }
}

@Observable
@MainActor
final class ProposalReopenViewModel {
    private(set) var response: ProposalReopenResponse?
    private(set) var isLoading = false
    var errorMessage: String?
    /// Ask's own view model: its confirm posts the proposal_id, so the
    /// stored proposal is what gets settled.
    let askViewModel = AskCavnarViewModel()
    private let client: APIClient

    init(client: APIClient = .shared) { self.client = client }

    func load(id: Int) async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let r: ProposalReopenResponse = try await client.send(
                "/mobile/api/ask-cavnar/proposals/\(id)", hapticOnError: false)
            response = r
            if !r.ok { errorMessage = r.error ?? "That proposal wasn\u{2019}t found." }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t reach Cavnar AI."
        }
    }
}

struct ProposalReopenSheet: View {
    let proposalId: Int
    @State private var viewModel = ProposalReopenViewModel()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if viewModel.isLoading && viewModel.response == nil {
                        CavnarSkeletonLines(widths: [0.5, 0.9, 0.7], lineHeight: 12, spacing: 10)
                    } else if let proposal = viewModel.response?.proposal {
                        ProposalCard(proposal: proposal, viewModel: viewModel.askViewModel)
                    } else if let line = viewModel.response?.settledLine {
                        Text(line.uppercased())
                            .font(.cavnarBody(12, weight: 700))
                            .tracking(1.4)
                            .foregroundStyle(Color.cavnarInk3)
                        if let summary = viewModel.response?.summary {
                            Text(summary)
                                .font(.cavnarHeadline(18))
                                .foregroundStyle(Color.cavnarInk)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    } else {
                        Text(viewModel.errorMessage ?? "That proposal wasn\u{2019}t found.")
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk2)
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .topLeading)
            }
            .accountSheetChrome("Waiting on you")
        }
        .task { await viewModel.load(id: proposalId) }
    }
}
