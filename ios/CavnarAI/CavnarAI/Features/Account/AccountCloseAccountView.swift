import SwiftUI

/// Opened from Account's "Close my account" row. Deliberately NOT
/// self-serve — clients sign a contract (DocuSign) to start service, so
/// canceling requires contacting Will directly. This sheet is purely
/// informational: no backend route, no state change, same tappable-mailto
/// pattern as Billing's "no active plan" contact link. Mirrors the exact
/// 30-day-notice policy copy dashboard.html's "Request cancellation" link
/// already uses, rather than inventing separate wording for this platform.
struct AccountCloseAccountView: View {
    let viewModel: AccountViewModel

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

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Close my account") {
                        GlowBadge(systemImage: "xmark.circle", size: 64)
                    } subtitle: {
                        Text("Cancellation goes through Will")
                    }

                    Text("Getting set up on Cavnar AI includes signing a service agreement, so canceling isn't something this app can do on its own — it needs to go through Will directly so billing and your account can be wound down properly. 30 days' written notice is required; your account stays active through the end of your current billing period plus 30 days after your request.")
                        .font(.cavnarBody(16))
                        .foregroundStyle(Color.cavnarInk3)

                    // ONE markdown string, not Text + Text concatenation —
                    // the latter silently drops SwiftUI's interactive-link
                    // hit-testing even though the styling still looks right
                    // (root-caused this session in Billing's identical link).
                    Text("Contact [will@cavnar.ai](\(mailtoLink)) to request cancellation.")
                        .font(.cavnarBody(16))
                        .tint(Color.cavnarEmber)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Close My Account")
        }
    }
}
