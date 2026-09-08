import SwiftUI

/// Opened from Account's "Close my account" row. Not self-serve deletion —
/// clients sign a contract (DocuSign) to start service, so Cavnar AI can't
/// deactivate one on its own — but tapping the button below is a real
/// request, not a mailto: link: it hits the server, is recorded, and emails
/// Will to start the 30-day wind-down. That distinction is Apple App Store
/// Review Guideline 5.1.1(v) — an app that supports account creation must
/// let the user initiate deletion from inside the app; a "please email us"
/// flow doesn't satisfy it outside a handful of regulated industries, which
/// this isn't. A direct mailto to Will stays underneath as one more way to
/// reach him, same tappable-link pattern used elsewhere in Account, but the
/// primary control above it is the actual initiation.
struct AccountCloseAccountView: View {
    let viewModel: AccountViewModel
    @State private var showingConfirm = false

    private var mailtoLink: String {
        let name = viewModel.summary?.profile.restaurantName ?? "my restaurant"
        let subject = "Cancel my Cavnar AI subscription"
        let body = "Hi Will,\n\nI would like to cancel my Cavnar AI subscription for \(name).\n\nPer the 30-day notice policy, I understand my account will remain active through the end of my current billing period and for 30 days after this notice."
        var components = URLComponents()
        components.scheme = "mailto"
        components.path = "will@cavnar.ai"
        components.queryItems = [
            URLQueryItem(name: "subject", value: subject),
            URLQueryItem(name: "body", value: body),
        ]
        return components.string ?? "mailto:will@cavnar.ai"
    }

    private var requestedAt: String? { viewModel.summary?.profile.deletionRequestedAt }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Close my account") {
                        GlowBadge(systemImage: "xmark.circle", size: 64)
                    } subtitle: {
                        Text(requestedAt != nil ? "Request received" : "Cancellation goes through Will")
                    }

                    Text("Getting set up on Cavnar AI includes signing a service agreement, so canceling isn't something this app can do on its own — it needs to go through Will directly so billing and your account can be wound down properly. 30 days' written notice is required; your account stays active through the end of your current billing period plus 30 days after your request.")
                        .font(.cavnarBody(16))
                        .foregroundStyle(Color.cavnarInk3)

                    if let requestedAt {
                        AccountSection(kicker: "Status") {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("Deletion requested").font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                                Text("Will was notified on \(requestedAt). He'll reach out to confirm and start winding things down.")
                                    .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                            }
                            .padding(.vertical, 9)
                        }
                    } else {
                        if let error = viewModel.deletionRequestError {
                            Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                        }
                        Button(role: .destructive) {
                            showingConfirm = true
                        } label: {
                            Group {
                                if viewModel.isRequestingDeletion {
                                    CavnarShimmerText(text: "Sending…")
                                } else {
                                    Text("Request account deletion")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isRequestingDeletion))
                        .disabled(viewModel.isRequestingDeletion)
                        .confirmationDialog(
                            "Request account deletion?",
                            isPresented: $showingConfirm,
                            titleVisibility: .visible
                        ) {
                            Button("Request deletion", role: .destructive) {
                                Task {
                                    if await viewModel.requestAccountDeletion() { Haptic.success() }
                                }
                            }
                            Button("Cancel", role: .cancel) {}
                        } message: {
                            Text("Will is notified right away to start the 30-day wind-down. Your account and data stay active until then.")
                        }
                    }

                    // A real Link, matching Help & FAQ's proven-working
                    // "Contact Will" and Billing's identical fix — markdown
                    // links embedded in Text never actually became tappable
                    // on a real device despite looking and coloring
                    // correctly, across multiple attempts. Link owns the tap
                    // gesture itself, so Text+Text concatenation for
                    // per-segment color is safe here.
                    if let url = URL(string: mailtoLink) {
                        Link(destination: url) {
                            Text(requestedAt != nil ? "Or contact " : "You can also contact ").foregroundStyle(Color.cavnarInk3)
                                + Text("will@cavnar.ai").foregroundStyle(Color.cavnarEmber)
                                + Text(" directly.").foregroundStyle(Color.cavnarInk3)
                        }
                        .font(.cavnarBody(16))
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Close My Account")
        }
    }
}
