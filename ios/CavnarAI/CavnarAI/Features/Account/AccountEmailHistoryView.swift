import SwiftUI

/// Opened from Account. Answers the question the app previously couldn't:
/// did the staff schedule / supplier order / reset code actually go out?
struct AccountEmailHistoryView: View {
    let viewModel: AccountViewModel

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if viewModel.isLoadingEmailHistory && viewModel.emailHistory.isEmpty {
                        CavnarLoadingOrb().padding(.top, 60).frame(maxWidth: .infinity)
                    } else if viewModel.emailHistory.isEmpty {
                        Text("No email sent yet.")
                            .font(.cavnar(.body))
                            .foregroundStyle(Color.cavnarInk2)
                            .padding(.top, 40)
                            .frame(maxWidth: .infinity)
                    } else {
                        AccountSection(kicker: "Everything sent for this restaurant") {
                            ForEach(Array(viewModel.emailHistory.enumerated()), id: \.element.id) { index, mail in
                                HStack(spacing: 12) {
                                    Image(systemName: mail.failed ? "exclamationmark.triangle.fill" : "envelope.fill")
                                        .font(.system(size: 15, weight: .medium))
                                        .foregroundStyle(mail.failed ? Color.cavnarRed : Color.cavnarInk2)
                                        .frame(width: 34, height: 34)
                                        .background(Color.white.opacity(0.05))
                                        .clipShape(RoundedRectangle(cornerRadius: 10))
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(mail.label)
                                            .cavnarText(.label)
                                        Text(mail.toEmail)
                                            .cavnarText(.secondary)
                                            .lineLimit(1)
                                        HomeMixedText.make(AccountRelativeTime.describe(mail.sentAt), role: .caption)
                                    }
                                    Spacer(minLength: 0)
                                    if mail.failed {
                                        Text(Self.statusWord(mail.status))
                                            .font(.cavnarBody(CavnarType.caption, weight: 700))
                                            .foregroundStyle(Color.cavnarRedText)
                                    }
                                }
                                .padding(.vertical, 10)
                                if index < viewModel.emailHistory.count - 1 { AccountRowDivider() }
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Email History")
            .task { await viewModel.loadEmailHistory() }
        }
    }

    /// A failed send's state in words — never the provider's raw status
    /// ("bounced", "complained", "delivery_delayed").
    static func statusWord(_ raw: String) -> String {
        switch raw.lowercased() {
        case "bounced", "bounce", "hard_bounce", "soft_bounce": return "Bounced"
        case "complained", "complaint", "spam": return "Marked as spam"
        case "delivery_delayed", "delayed", "deferred": return "Delayed"
        case "failed", "error", "send_failed", "rejected", "dropped": return "Didn\u{2019}t send"
        case "suppressed", "blocked": return "Blocked"
        default: return "Didn\u{2019}t arrive"
        }
    }
}
