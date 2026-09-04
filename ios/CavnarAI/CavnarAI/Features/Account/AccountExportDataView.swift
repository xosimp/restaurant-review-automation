import SwiftUI

/// Opened from Account's "Export my data" row (More group). Pick what to
/// include — reviews, labor, food cost, settings — and it's emailed as one
/// message with an attachment per item. Data retention lives here too:
/// it's the other half of "what happens to my data".
struct AccountExportDataView: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var scopes: Set<String> = ["reviews"]
    @State private var retentionPosted: String?

    private static let scopeOptions: [(key: String, label: String, detail: String)] = [
        ("reviews", "Reviews", "Date, rating, text, response status"),
        ("labor", "Labor", "Weekly hours and labor cost history"),
        ("food_cost", "Food cost", "Ingredients, on-hand, par, cost, waste"),
        ("settings", "Settings", "Every self-serve setting, as JSON"),
    ]
    private static let retentionOptions: [(months: Int, label: String)] = [
        (0, "Keep everything"), (6, "6 months"), (12, "12 months"), (24, "2 years"), (36, "3 years"),
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Your data") {
                        GlowBadge(systemImage: "square.and.arrow.up", size: 64)
                    } subtitle: {
                        Text("Export it, or set how long it's kept")
                    }

                    AccountSection(kicker: "Include in the export") {
                        ForEach(Array(Self.scopeOptions.enumerated()), id: \.element.key) { index, option in
                            Button {
                                Haptic.selection()
                                if scopes.contains(option.key) { scopes.remove(option.key) } else { scopes.insert(option.key) }
                            } label: {
                                HStack(spacing: 12) {
                                    Image(systemName: scopes.contains(option.key) ? "checkmark.circle.fill" : "circle")
                                        .font(.system(size: 20, weight: .semibold))
                                        .foregroundStyle(scopes.contains(option.key) ? Color.cavnarEmber : Color.cavnarInk3.opacity(0.6))
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(option.label).font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                                        Text(option.detail).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                                    }
                                    Spacer(minLength: 0)
                                }
                                .padding(.vertical, 9)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            if index < Self.scopeOptions.count - 1 { AccountRowDivider() }
                        }
                    }

                    if viewModel.exportDataSucceeded {
                        Text("Sent — check your inbox.")
                            .font(.cavnarBody(15, weight: 600))
                            .foregroundStyle(Color.cavnarGreen)
                    } else if let error = viewModel.exportDataError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    Button {
                        Task {
                            await viewModel.exportData(scopes: Self.scopeOptions.map(\.key).filter { scopes.contains($0) })
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
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isExportingData || scopes.isEmpty))
                    .disabled(viewModel.isExportingData || scopes.isEmpty)

                    AccountSection(kicker: "Data retention") {
                        AccountKVRow(label: "Keep reviews for") {
                            Picker("", selection: Binding(
                                get: { viewModel.summary?.data.retentionMonths ?? 0 },
                                set: { months in
                                    Haptic.selection()
                                    Task {
                                        if await viewModel.setDataRetention(months: months) {
                                            Haptic.success()
                                            retentionPosted = months == 0 ? "Keeping everything" : "Older reviews will be removed"
                                        }
                                    }
                                }
                            )) {
                                ForEach(Self.retentionOptions, id: \.months) { Text($0.label).tag($0.months) }
                            }
                            .tint(Color.cavnarEmber)
                        }
                        Text("Reviews older than this are removed nightly. Nothing else is touched.")
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3)
                            .padding(.vertical, 9)
                        if let error = viewModel.retentionError {
                            Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed).padding(.bottom, 6)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Your Data")
            .cavnarPostedOverlay(retentionPosted) { retentionPosted = nil }
        }
    }
}
