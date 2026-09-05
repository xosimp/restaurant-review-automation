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
                        CavnarLoadingSeal().padding(.top, 60).frame(maxWidth: .infinity)
                    } else if viewModel.emailHistory.isEmpty {
                        Text("No email sent yet.")
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk3)
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
                                            .font(.cavnarBody(16, weight: 700))
                                            .foregroundStyle(Color.cavnarInk)
                                        Text(mail.toEmail)
                                            .font(.cavnarBody(13.5))
                                            .foregroundStyle(Color.cavnarInk3)
                                            .lineLimit(1)
                                        Text(AccountRelativeTime.describe(mail.sentAt))
                                            .font(.cavnarNumber(13.5))
                                            .foregroundStyle(Color.cavnarInk3)
                                    }
                                    Spacer(minLength: 0)
                                    if mail.failed {
                                        Text(mail.status.capitalized)
                                            .font(.cavnarBody(12.5, weight: 700))
                                            .foregroundStyle(Color.cavnarRed)
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
}
