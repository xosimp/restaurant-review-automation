import SwiftUI

/// Opened from Security's "Sign-in activity" row. Distinct from the
/// Devices list on that same sheet: Devices only shows currently-live
/// sessions, this shows every past login — including ones whose session
/// has since expired, been revoked, or been replaced by a same-device
/// re-login — since auth.py writes an append-only login_history row that
/// nothing ever deletes.
struct AccountSignInHistoryView: View {
    let viewModel: AccountViewModel

    private func symbol(for entry: LoginHistoryEntry) -> String {
        if entry.deviceType == "ios" || entry.label.hasPrefix("iPhone") { return "iphone" }
        if entry.label.hasPrefix("iPad") { return "ipad" }
        if entry.label == "Mac" { return "laptopcomputer" }
        if entry.label == "Windows" { return "desktopcomputer" }
        return "globe"
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if viewModel.isLoadingLoginHistory && viewModel.loginHistory.isEmpty {
                        CavnarLoadingSeal().padding(.top, 60).frame(maxWidth: .infinity)
                    } else if viewModel.loginHistory.isEmpty {
                        Text("No sign-in activity yet.")
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk3)
                            .padding(.top, 40)
                            .frame(maxWidth: .infinity)
                    } else {
                        AccountSection(kicker: "Every sign-in, oldest sessions included") {
                            ForEach(Array(viewModel.loginHistory.enumerated()), id: \.element.id) { index, entry in
                                HStack(spacing: 12) {
                                    Image(systemName: symbol(for: entry))
                                        .font(.system(size: 16, weight: .medium))
                                        .foregroundStyle(Color.cavnarInk2)
                                        .frame(width: 34, height: 34)
                                        .background(Color.white.opacity(0.05))
                                        .clipShape(RoundedRectangle(cornerRadius: 10))
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(entry.label).font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                                        Text(AccountRelativeTime.describe(entry.createdAt))
                                            .font(.cavnarNumber(14))
                                            .foregroundStyle(Color.cavnarInk3)
                                    }
                                    Spacer(minLength: 0)
                                }
                                .padding(.vertical, 10)
                                if index < viewModel.loginHistory.count - 1 { AccountRowDivider() }
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Sign-In Activity")
            .task { await viewModel.loadLoginHistory() }
        }
    }
}
