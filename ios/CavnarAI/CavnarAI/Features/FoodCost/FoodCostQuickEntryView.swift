import SwiftUI

private enum FoodCostSubTab: String, CaseIterable, Identifiable {
    case tracker = "Tracker"
    case analytics = "Analytics"
    var id: String { rawValue }
}

struct FoodCostQuickEntryView: View {
    @State private var viewModel = FoodCostQuickEntryViewModel()
    @State private var analyticsViewModel = FoodCostAnalyticsViewModel()
    @State private var subTab: FoodCostSubTab = .tracker

    var body: some View {
        // No NavigationStack of its own — pushed inside Home's or the
        // Modules tab's stack now, not a tab root.
        VStack(spacing: 0) {
            CavnarSegmentedControl(selection: $subTab, options: FoodCostSubTab.allCases) { $0.rawValue }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 16)

            if subTab == .tracker {
                tracker
            } else {
                FoodCostAnalyticsSection(viewModel: analyticsViewModel)
            }
        }
        .cavnarModuleBackground()
        .navigationTitle("Food Cost")
        .navigationBarTitleDisplayMode(.inline)
        .cavnarEmberTitle("Food Cost")
        .cavnarEmberBackButton()
        .task {
            await analyticsViewModel.load()
        }
    }

    private var tracker: some View {
        Form {
                Section {
                    Text("Fill in this week's price per unit right after an invoice arrives.")
                        .font(.cavnarBody(12))
                        .foregroundStyle(Color.cavnarInk3)
                } header: {
                    Text("Key Ingredient Prices")
                }

                Section {
                    ForEach($viewModel.items) { $item in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                TextField("Item name", text: $item.name)
                                    .font(.cavnarBody(14, weight: 600))
                                TextField("unit", text: $item.unit)
                                    .font(.cavnarBody(12))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .frame(width: 60)
                            }
                            HStack {
                                HStack(spacing: 4) {
                                    Text("$")
                                        .font(.cavnarNumber(13))
                                        .foregroundStyle(Color.cavnarInk3)
                                    TextField("price/unit", text: $item.priceText)
                                        .keyboardType(.decimalPad)
                                        .font(.cavnarNumber(15))
                                }
                                Spacer()
                                HStack(spacing: 4) {
                                    TextField("wk usage", text: $item.usageText)
                                        .keyboardType(.decimalPad)
                                        .font(.cavnarNumber(15))
                                        .multilineTextAlignment(.trailing)
                                    Text("used/wk")
                                        .font(.cavnarBody(11))
                                        .foregroundStyle(Color.cavnarInk3)
                                }
                            }
                        }
                        .padding(.vertical, 4)
                    }
                    .onDelete { indices in
                        Haptic.selection()
                        viewModel.items.remove(atOffsets: indices)
                    }

                    Button {
                        viewModel.addCustomRow()
                    } label: {
                        Label("Add ingredient", systemImage: "plus.circle")
                    }
                    .font(.cavnarBody(13, weight: 600))
                }

                if viewModel.didSubmit {
                    Section("This Week's Result") {
                        resultSummary
                    }
                }

                if let error = viewModel.errorMessage {
                    Text(error)
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarRed)
                }

                Section {
                    Button {
                        Task { await viewModel.submit() }
                    } label: {
                        if viewModel.isSubmitting {
                            HStack { Spacer(); ProgressView(); Spacer() }
                        } else {
                            Text("Submit this week's prices")
                                .frame(maxWidth: .infinity)
                        }
                    }
                    .font(.cavnarBody(15, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
                    .disabled(!viewModel.canSubmit)
                }
        }
        .scrollContentBackground(.hidden)
    }

    @ViewBuilder
    private var resultSummary: some View {
        if viewModel.drift.isEmpty {
            Label("Prices stable vs. last submission", systemImage: "checkmark.circle.fill")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarGreen)
        } else {
            ForEach(viewModel.drift) { drift in
                HStack {
                    Text(drift.name)
                        .font(.cavnarBody(13, weight: 600))
                    Spacer()
                    Text(String(format: "%@%.1f%%", drift.direction == "up" ? "↑ " : "↓ ", abs(drift.pctChange)))
                        .font(.cavnarNumber(13))
                        .foregroundStyle(drift.direction == "up" ? Color.cavnarRed : Color.cavnarGreen)
                }
            }
            if let total = viewModel.totalWeeklyImpact, total > 0 {
                (Text("Est. ") + Text("+$\(Int(total))/week").font(.cavnarNumber(12, weight: 600)) + Text(" from price increases"))
                    .font(.cavnarBody(12, weight: 600))
                    .foregroundStyle(Color.cavnarAmber)
            }
        }
    }
}
