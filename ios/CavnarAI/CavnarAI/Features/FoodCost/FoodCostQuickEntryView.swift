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

/// Which field, on which card, currently has focus — lives here (not
/// inside IngredientCard) so "Add ingredient" can drive focus onto a
/// specific new card's name field from outside it, and so one shared
/// keyboard-dismiss toolbar covers every card instead of each row needing
/// its own.
private enum CarouselField: Hashable {
    case name(UUID), unit(UUID), price(UUID), usage(UUID)
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

    @FocusState private var focusedField: CarouselField?

    private var windowHeight: CGFloat {
        cardHeight * visibleCardCount + cardSpacing * (visibleCardCount - 1) + verticalInset * 2
    }

    var body: some View {
        ScrollViewReader { proxy in
            VStack(spacing: 10) {
                ScrollView(showsIndicators: false) {
                    LazyVStack(spacing: cardSpacing) {
                        ForEach($items) { $item in
                            IngredientCard(item: $item, height: cardHeight, focusedField: $focusedField) {
                                Haptic.selection()
                                withAnimation(.easeOut(duration: 0.22)) {
                                    items.removeAll { $0.id == item.id }
                                }
                            }
                            .id(item.id)
                            .transition(.opacity.combined(with: .scale(scale: 0.85)))
                            // Toned down from the first pass — opacity/blur
                            // dropping too far at the edges, on top of each
                            // card's own gradient already fading toward
                            // transparent there, read as the cards
                            // smearing into a dark blur rather than gently
                            // dissolving.
                            .scrollTransition(.interactive(timingCurve: .easeInOut), axis: .vertical) { content, phase in
                                let d = min(abs(phase.value), 1)
                                return content
                                    .opacity(1 - d * 0.55)
                                    .scaleEffect(1 - d * 0.06)
                                    .blur(radius: d * 1.2)
                                    .offset(y: phase.value * 16)
                            }
                        }
                    }
                    .padding(.vertical, verticalInset)
                }
                .frame(height: windowHeight)
                // Soft top/bottom dissolve instead of a hard clip edge —
                // reinforces the scrollTransition fade rather than cutting
                // cards off mid-fade right at the window boundary. Floored
                // at 0.5 alpha (not fully clear) and a wider stable zone
                // (22%–78%, was 10%–88%) so edge cards stay legibly dimmed
                // instead of nearly vanishing — same "too much" fix as the
                // scrollTransition above.
                .mask(
                    LinearGradient(
                        stops: [
                            .init(color: .black.opacity(0.5), location: 0),
                            .init(color: .black, location: 0.22),
                            .init(color: .black, location: 0.78),
                            .init(color: .black.opacity(0.5), location: 1),
                        ],
                        startPoint: .top, endPoint: .bottom
                    )
                )
                // A visible hint that this window scrolls — same SWIPE +
                // arrow language as Home's NeedsAttentionCarousel, rotated
                // to point down since this carousel is vertical. Sits above
                // the mask (applied after it in the chain) so it's never
                // itself faded, and only shows once there's actually more
                // than the default 3 to reveal.
                .overlay(alignment: .topTrailing) {
                    if items.count > Int(visibleCardCount) {
                        HStack(spacing: 4) {
                            Text("SWIPE")
                                .font(.cavnarBody(9, weight: 700))
                                .tracking(1.5)
                            Image(systemName: "arrow.down")
                                .font(.system(size: 10, weight: .bold))
                        }
                        .foregroundStyle(Color.cavnarEmber2.opacity(0.8))
                        .padding(.top, 4)
                        .padding(.trailing, 6)
                    }
                }

                Button {
                    Haptic.light()
                    let newItem = onAddRow()
                    // completionCriteria: .logicallyComplete (not the
                    // default) fires `completion` once the scroll's real
                    // motion settles, not the instant the animation block
                    // returns — focusing the field any earlier would ask
                    // for focus on a row that hasn't scrolled into place
                    // yet.
                    withAnimation(.spring(response: 0.5, dampingFraction: 0.85), completionCriteria: .logicallyComplete) {
                        proxy.scrollTo(newItem.id, anchor: .center)
                    } completion: {
                        focusedField = .name(newItem.id)
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
        // decimalPad has no built-in Done key — without this there was no
        // way to dismiss the keyboard short of navigating away entirely.
        // One shared bar covers every card's price/usage/name/unit field
        // since focusedField lives here, not per-card.
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") { focusedField = nil }
                    .font(.cavnarBody(14, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
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
    var focusedField: FocusState<CarouselField?>.Binding
    var onDelete: () -> Void

    private var isNameFocused: Bool { focusedField.wrappedValue == .name(item.id) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .bottom, spacing: 8) {
                // A hairline underline that lights up ember on focus —
                // same convention as CavnarFloatingField everywhere else
                // in the app — is what actually signals "this is editable"
                // at a glance; a plain TextField with no chrome reads
                // identically to static Text until someone happens to tap it.
                VStack(alignment: .leading, spacing: 4) {
                    TextField("Ingredient name", text: $item.name)
                        .font(.cavnarHeadline(17, weight: .semiBold))
                        .foregroundStyle(Color.cavnarInk)
                        .lineLimit(1)
                        .focused(focusedField, equals: .name(item.id))
                    Rectangle()
                        .fill(isNameFocused ? Color.cavnarEmber2 : Color.cavnarInk.opacity(0.3))
                        .frame(height: isNameFocused ? 1.5 : 1)
                }
                TextField("unit", text: $item.unit)
                    .font(.cavnarBody(10, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .tracking(0.4)
                    .multilineTextAlignment(.center)
                    .frame(width: 38)
                    .focused(focusedField, equals: .unit(item.id))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 5)
                    .background(Color.black.opacity(0.35))
                    .overlay(Capsule().strokeBorder(Color.cavnarInk.opacity(0.18), lineWidth: 1))
                    .clipShape(Capsule())
                    .padding(.bottom, 2)
                Button(action: onDelete) {
                    Image(systemName: "xmark")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Color.cavnarInk.opacity(0.6))
                        .frame(width: 22, height: 22)
                        .background(Color.black.opacity(0.3), in: Circle())
                }
                .padding(.bottom, 1)
            }
            HStack(spacing: 24) {
                statField(label: "PRICE", prefix: "$", text: $item.priceText, field: .price(item.id))
                statField(label: "USED / WK", prefix: nil, text: $item.usageText, field: .usage(item.id))
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

    private func statField(label: String, prefix: String?, text: Binding<String>, field: CarouselField) -> some View {
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
                    .focused(focusedField, equals: field)
                    .fixedSize()
            }
        }
    }
}
