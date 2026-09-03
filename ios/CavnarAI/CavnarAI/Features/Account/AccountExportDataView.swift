import SwiftUI

/// Opened from Account's "Export my data" row (More group). Scope is
/// deliberately narrow — reviews only (date, rating, text, response
/// status) as a CSV, emailed to whoever's logged in — not a full
/// multi-module data dump.
struct AccountExportDataView: View {
    let viewModel: AccountViewModel

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Export my data") {
                        GlowBadge(systemImage: "square.and.arrow.up", size: 64)
                    } subtitle: {
                        Text("A CSV of your reviews")
                    }

                    Text("We'll email a CSV of your reviews — date, rating, review text, and response status — to your account email.")
                        .font(.cavnarBody(16))
                        .foregroundStyle(Color.cavnarInk3)

                    if viewModel.exportDataSucceeded {
                        Text("Sent — check your inbox.")
                            .font(.cavnarBody(15, weight: 600))
                            .foregroundStyle(Color.cavnarGreen)
                    } else if let error = viewModel.exportDataError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    Button {
                        Task {
                            await viewModel.exportData()
                            if viewModel.exportDataSucceeded { Haptic.success() }
                        }
                    } label: {
                        Group {
                            if viewModel.isExportingData {
                                CavnarShimmerText(text: "Sending…")
                            } else {
                                Text("Email my data")
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isExportingData))
                    .disabled(viewModel.isExportingData)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Export Data")
        }
    }
}
