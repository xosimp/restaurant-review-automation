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
        .cavnarEmberBackButton()
        .task {
            await analyticsViewModel.load()
        }
    }

    private var tracker: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("KEY INGREDIENT PRICES")
                        .font(.cavnarBody(10, weight: 700))
                        .tracking(1.2)
                        .foregroundStyle(Color.cavnarEmber2)
                    Text("Fill in this week's price per unit right after an invoice arrives.")
                        .font(.cavnarBody(12))
                        .foregroundStyle(Color.cavnarInk3)
                }

                IngredientCarousel(items: $viewModel.items, onAddRow: { viewModel.addCustomRow() })

                if let error = viewModel.errorMessage {
                    Text(error)
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarRed)
                }

                Button {
                    Task { await viewModel.submit() }
                } label: {
                    if viewModel.isSubmitting {
                        ProgressView().tint(Color.cavnarInk)
                    } else {
                        Text("Submit this week's prices")
                    }
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !viewModel.canSubmit))
                .disabled(!viewModel.canSubmit)

                if viewModel.didSubmit {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("THIS WEEK'S RESULT")
                            .font(.cavnarBody(10, weight: 700))
                            .tracking(1.2)
                            .foregroundStyle(Color.cavnarGreen)
                        resultSummary
                    }
                    .cavnarCard()
                }
            }
            .padding(20)
        }
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

/// A fixed-height window showing 3 ingredient cards at a time, scrolling
/// through the rest — not a plain full-length list. Cards crossing the
/// window's top edge fade/scale/blur away as they exit; cards rising into
/// view from the bottom (from behind "Add ingredient", pinned directly
/// under the window) fade and settle into place the same way in reverse.
/// .scrollTransition(.interactive) ties every bit of that motion directly
/// to the user's finger — no separate re-triggered animation chasing the
/// scroll position — which is what makes it read as smooth rather than a
/// canned effect layered on top of a plain scroll.
private struct IngredientCarousel: View {
    @Binding var items: [FoodCostItem]
    var onAddRow: () -> FoodCostItem

    private let cardHeight: CGFloat = 108
    private let cardSpacing: CGFloat = 12
    private let visibleCardCount: CGFloat = 3
    private let verticalInset: CGFloat = 14

    private var windowHeight: CGFloat {
        cardHeight * visibleCardCount + cardSpacing * (visibleCardCount - 1) + verticalInset * 2
    }

    var body: some View {
        ScrollViewReader { proxy in
            VStack(spacing: 10) {
                ScrollView(showsIndicators: false) {
                    LazyVStack(spacing: cardSpacing) {
                        ForEach($items) { $item in
                            IngredientCard(item: $item, height: cardHeight) {
                                Haptic.selection()
                                withAnimation(.easeOut(duration: 0.22)) {
                                    items.removeAll { $0.id == item.id }
                                }
                            }
                            .id(item.id)
                            .transition(.opacity.combined(with: .scale(scale: 0.85)))
                            .scrollTransition(.interactive(timingCurve: .easeInOut), axis: .vertical) { content, phase in
                                return content
                                    .opacity(1 - min(abs(phase.value), 1) * 0.92)
                                    .scaleEffect(1 - min(abs(phase.value), 1) * 0.1)
                                    .blur(radius: min(abs(phase.value), 1) * 2.5)
                                    .offset(y: phase.value * 22)
                            }
                        }
                    }
                    .padding(.vertical, verticalInset)
                }
                .frame(height: windowHeight)
                // Soft top/bottom dissolve instead of a hard clip edge —
                // reinforces the scrollTransition fade rather than cutting
                // cards off mid-fade right at the window boundary.
                .mask(
                    LinearGradient(
                        stops: [
                            .init(color: .clear, location: 0),
                            .init(color: .black, location: 0.1),
                            .init(color: .black, location: 0.88),
                            .init(color: .clear, location: 1),
                        ],
                        startPoint: .top, endPoint: .bottom
                    )
                )

                Button {
                    Haptic.light()
                    let newItem = onAddRow()
                    withAnimation(.spring(response: 0.5, dampingFraction: 0.85)) {
                        proxy.scrollTo(newItem.id, anchor: .bottom)
                    }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "plus.circle.fill")
                        Text("Add ingredient")
                    }
                    .font(.cavnarBody(13, weight: 600))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                }
                .background(
                    RoundedRectangle(cornerRadius: CavnarRadius.card)
                        .strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
                        .foregroundStyle(Color.cavnarEmber2.opacity(0.4))
                )
            }
        }
    }
}

/// One ingredient's editable card — the same ember-fade gradient, inset
/// border, and drop shadow as Schedule History's rows (via CavnarEmberFade,
/// shared so the two can't visually drift apart), restyled as a data-entry
/// surface: name and unit stay tappable/editable exactly like the old
/// Form's text fields did, just no longer inside a plain gray Form row.
private struct IngredientCard: View {
    @Binding var item: FoodCostItem
    let height: CGFloat
    var onDelete: () -> Void

    private enum Field { case name, unit, price, usage }
    @FocusState private var focusedField: Field?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                TextField("Ingredient name", text: $item.name)
                    .font(.cavnarHeadline(17, weight: .semiBold))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                    .focused($focusedField, equals: .name)
                Spacer(minLength: 8)
                TextField("unit", text: $item.unit)
                    .font(.cavnarBody(10, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .tracking(0.4)
                    .multilineTextAlignment(.center)
                    .frame(width: 38)
                    .focused($focusedField, equals: .unit)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 5)
                    .background(Color.black.opacity(0.35))
                    .overlay(Capsule().strokeBorder(Color.cavnarInk.opacity(0.18), lineWidth: 1))
                    .clipShape(Capsule())
                Button(action: onDelete) {
                    Image(systemName: "xmark")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Color.cavnarInk.opacity(0.6))
                        .frame(width: 22, height: 22)
                        .background(Color.black.opacity(0.3), in: Circle())
                }
            }
            HStack(spacing: 24) {
                statField(label: "PRICE", prefix: "$", text: $item.priceText, field: .price)
                statField(label: "USED / WK", prefix: nil, text: $item.usageText, field: .usage)
                Spacer(minLength: 0)
            }
        }
        .padding(15)
        .frame(height: height)
        .background(
            ZStack {
                Color.cavnarPaper
                CavnarEmberFade.horizontal
            }
        )
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card)
                .inset(by: 1)
                .strokeBorder(CavnarEmberFade.horizontal, lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
        .shadow(color: .black.opacity(0.4), radius: 8, x: 0, y: 5)
    }

    private func statField(label: String, prefix: String?, text: Binding<String>, field: Field) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .font(.cavnarBody(8.5, weight: 700))
                .tracking(0.6)
                .foregroundStyle(Color.cavnarInk.opacity(0.65))
            HStack(spacing: 2) {
                if let prefix {
                    Text(prefix)
                        .font(.cavnarNumber(13, weight: 500))
                        .foregroundStyle(Color.cavnarInk.opacity(0.8))
                }
                TextField("0", text: text)
                    .keyboardType(.decimalPad)
                    .font(.cavnarNumber(17, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .focused($focusedField, equals: field)
                    .fixedSize()
            }
        }
    }
}
