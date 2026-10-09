import SwiftUI
import Observation

/// "Publish N ready" from the Reviews inbox (parity audit 10/7/26 #34) —
/// Home's publish-all, reused: the same confirm card the web and Ask render
/// (/command/propose → approve_all_reviews, no model call), every reply that
/// would post in its own words and how many the public-reply check holds
/// back, confirmed through Ask's own route. An older server without the
/// command route gets the count confirm and the same approve-all call Home
/// makes. Nothing posts until the owner confirms here.
@Observable
@MainActor
final class PublishReadyViewModel {
    private(set) var proposal: AskProposal?
    private(set) var isLoading = false
    private(set) var isPublishing = false
    var errorMessage: String?
    /// Ask's own view model: the proposal's Confirm posts its proposal_id.
    let askViewModel = AskCavnarViewModel()
    private let client: APIClient

    init(client: APIClient = .shared) { self.client = client }

    private struct ProposeBody: Encodable {
        let action: String
        let args: [String: String]
    }

    /// The same body Home sends (HomeViewModel.proposePublish).
    static func proposeBody() -> Data? {
        try? JSONEncoder().encode(ProposeBody(action: "approve_all_reviews", args: [:]))
    }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        let r: CommandProposeResponse? = try? await client.send(
            "/mobile/api/command/propose", method: .post,
            body: ProposeBody(action: "approve_all_reviews", args: [:]), hapticOnError: false)
        if let r, r.ok { proposal = r.proposal }
    }

    private struct PublishBody: Encodable { let limit: Int }

    /// The count confirm's Post (an older server): approve-all, as Home.
    func publish(limit: Int) async -> BulkPublishResult? {
        isPublishing = true
        errorMessage = nil
        defer { isPublishing = false }
        do {
            return try await client.send("/mobile/api/reviews/approve-all", method: .post,
                                         body: PublishBody(limit: limit), retryTransient: false)
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t publish \u{2014} try again."
        }
        return nil
    }
}

struct PublishReadySheet: View {
    let count: Int
    /// Called with the posted check's words once something went out.
    var onPublished: (String) -> Void
    @State private var viewModel = PublishReadyViewModel()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let p = viewModel.proposal {
                        ProposalCard(proposal: p, viewModel: viewModel.askViewModel) {
                            Haptic.success()
                            onPublished("Replies approved")
                            dismiss()
                        }
                    } else if viewModel.isLoading {
                        CavnarShimmerText(text: "Reading the replies\u{2026}", color: Color.cavnarInk)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 18)
                    } else {
                        countConfirm
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Publish replies")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Publish replies") }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                        .disabled(viewModel.isPublishing)
                }
            }
            .task { await viewModel.load() }
        }
    }

    /// Where a bulk publish sends each reply, by platform.
    static let whereTheyGo = "Replies to Google reviews go live on Google under your name, once Google is connected. "
        + "Replies to Yelp and other sites are approved for you to post there. Newest first; a flagged or urgent "
        + "reply is never in a bulk publish."

    /// The count-only confirm — an older server without /command/propose.
    private var countConfirm: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.m) {
            // Named by where each goes (readability round 10/8/26 #54): a
            // Google review's reply posts to Google once it is connected;
            // any other site's reply is approved for the owner to post.
            CavnarMixedText("Approve and post \(count) \(count == 1 ? "reply" : "replies")?", role: .headline)
            Text(Self.whereTheyGo)
                .cavnarText(.body)
                .fixedSize(horizontal: false, vertical: true)
            if let error = viewModel.errorMessage {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            }
            Button {
                Task {
                    guard let r = await viewModel.publish(limit: count) else { return }
                    Haptic.success()
                    onPublished(r.posted > 0 ? "Published \(r.posted) to Google"
                                : "Approved \(r.approved) \(r.approved == 1 ? "reply" : "replies")")
                    dismiss()
                }
            } label: {
                Group {
                    if viewModel.isPublishing {
                        CavnarShimmerText(text: "Publishing\u{2026}", color: Color.white)
                    } else {
                        HomeMixedText.make("Approve \(count) \(count == 1 ? "reply" : "replies")", role: .label,
                                           color: .white, numberColor: .white)
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: count == 0 || viewModel.isPublishing))
            .disabled(count == 0 || viewModel.isPublishing)
        }
    }
}
