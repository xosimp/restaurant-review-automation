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
    @State private var showSuccessToast = false

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
        // The left-swipe counterpart to the system's own swipe-right-to-
        // go-back gesture — jumps straight to this module's Analytics tab
        // from anywhere on screen, same as every other module.
        .cavnarSwipeToAnalytics($subTab, analyticsTab: .analytics)
        .overlay(alignment: .top) { successToast }
        .task {
            await analyticsViewModel.load()
        }
    }

    @ViewBuilder
    private var successToast: some View {
        if showSuccessToast {
            HStack(spacing: 10) {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(Color.cavnarGreen)
                Text("This week's prices submitted")
                    .font(.cavnarBody(13, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 13)
            .background(Color.cavnarGreenBg)
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .strokeBorder(Color.cavnarGreen.opacity(0.55), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            .shadow(color: .black.opacity(0.35), radius: 16, x: 0, y: 6)
            .padding(.top, 8)
            .transition(.asymmetric(
                insertion: .move(edge: .top).combined(with: .opacity),
                removal: .opacity
            ))
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

                HoldToSubmitButton(
                    isSubmitting: viewModel.isSubmitting,
                    didSubmit: viewModel.didSubmit,
                    canSubmit: viewModel.canSubmit
                ) {
                    Task {
                        await viewModel.submit()
                        guard viewModel.didSubmit else { return }
                        withAnimation(.spring(response: 0.45, dampingFraction: 0.75)) {
                            showSuccessToast = true
                        }
                        try? await Task.sleep(nanoseconds: 2_800_000_000)
                        withAnimation(.easeOut(duration: 0.4)) {
                            showSuccessToast = false
                        }
                    }
                }

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

/// A press-and-hold confirm button, not a plain tap — submitting overwrites
/// last week's prices, and a plain-tap "Submit" was one careless thumb away
/// from doing that by accident. Holding fills the button with a progress
/// sweep timed to exactly 3 seconds; releasing early cancels and resets it.
/// Once actually submitted, the button itself becomes the "done" indicator
/// (green, checkmark, no longer interactive) rather than silently returning
/// to its normal state as if nothing happened.
private struct HoldToSubmitButton: View {
    let isSubmitting: Bool
    let didSubmit: Bool
    let canSubmit: Bool
    let onConfirm: () -> Void

    private let holdDuration: Double = 3.0

    @State private var isHolding = false
    @State private var holdProgress: CGFloat = 0
    @State private var holdTask: Task<Void, Never>?

    var body: some View {
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .fill(didSubmit ? Color.cavnarGreen.opacity(0.16) : Color.cavnarEmber.opacity(0.28))

            if !didSubmit {
                GeometryReader { geo in
                    RoundedRectangle(cornerRadius: CavnarRadius.control)
                        .fill(Color.cavnarEmber.opacity(0.92))
                        .frame(width: geo.size.width * holdProgress)
                }
            }

            HStack(spacing: 8) {
                Spacer(minLength: 0)
                if didSubmit {
                    Image(systemName: "checkmark.circle.fill")
                    Text("Submitted")
                } else if isSubmitting {
                    ProgressView().tint(Color.cavnarInk)
                } else {
                    Text("Hold for 3 seconds to submit")
                }
                Spacer(minLength: 0)
            }
            .font(.cavnarBody(15, weight: 600))
            .foregroundStyle(didSubmit ? Color.cavnarGreen : Color.cavnarInk)
            .padding(.vertical, 14)
        }
        .frame(height: 52)
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(didSubmit ? Color.cavnarGreen.opacity(0.5) : Color.cavnarEmber.opacity(0.6), lineWidth: 1.5)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
        .opacity(canSubmit || didSubmit ? 1 : 0.5)
        .scaleEffect(isHolding ? 0.98 : 1)
        .animation(.easeOut(duration: 0.15), value: isHolding)
        .gesture(
            DragGesture(minimumDistance: 0)
                .onChanged { _ in
                    guard canSubmit, !didSubmit, !isSubmitting, !isHolding else { return }
                    startHold()
                }
                .onEnded { _ in cancelHold() }
        )
    }

    private func startHold() {
        Haptic.light()
        isHolding = true
        withAnimation(.linear(duration: holdDuration)) {
            holdProgress = 1
        }
        holdTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(holdDuration * 1_000_000_000))
            guard !Task.isCancelled else { return }
            isHolding = false
            Haptic.success()
            onConfirm()
        }
    }

    private func cancelHold() {
        guard isHolding else { return }
        holdTask?.cancel()
        holdTask = nil
        isHolding = false
        withAnimation(.easeOut(duration: 0.25)) {
            holdProgress = 0
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
                // A visible hint that this window scrolls — same SWIPE +
                // arrow language as Home's NeedsAttentionCarousel, rotated
                // to point down since this carousel is vertical. Its own
                // row above the window (not an overlay on top of it) so it
                // sits in genuinely empty space instead of drawing over
                // the first card — an overlay here only cleared the card
                // by a few points no matter how much top padding it got,
                // since the card itself starts rendering right at the
                // window's edge.
                if items.count > Int(visibleCardCount) {
                    HStack(spacing: 4) {
                        Spacer()
                        Text("SWIPE")
                            .font(.cavnarBody(9, weight: 700))
                            .tracking(1.5)
                        Image(systemName: "arrow.down")
                            .font(.system(size: 10, weight: .bold))
                    }
                    .foregroundStyle(Color.cavnarEmber2.opacity(0.8))
                    .padding(.trailing, 4)
                }

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

                Button {
                    Haptic.light()
                    let newItem = onAddRow()
                    withAnimation(.spring(response: 0.5, dampingFraction: 0.85)) {
                        proxy.scrollTo(newItem.id, anchor: .center)
                    }
                    // withAnimation's completion-callback overload doesn't
                    // reliably track ScrollViewProxy.scrollTo's motion (it
                    // isn't a tracked animatable value the way a plain
                    // state change is) — it was firing focus essentially
                    // immediately, before the scroll had visibly moved, so
                    // the keyboard appeared and the card snapped into
                    // place in the same instant instead of scroll-then-
                    // focus. A fixed delay comfortably past the spring's
                    // settle time is the reliable fix; once focus actually
                    // lands, iOS's own keyboard avoidance takes over to
                    // keep the field clear of the keyboard.
                    Task { @MainActor in
                        try? await Task.sleep(nanoseconds: 450_000_000)
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
        // since focusedField lives here, not per-card. Vertical padding
        // lifts the button clear of the bar's own top edge (which sits
        // flush against the keyboard) instead of crowding it.
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                HStack {
                    Spacer()
                    Button("Done") { focusedField = nil }
                        .font(.cavnarBody(14, weight: 700))
                        .foregroundStyle(Color.cavnarEmber)
                }
                .padding(.vertical, 10)
                .padding(.trailing, 2)
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
                    HStack(spacing: 6) {
                        TextField("Ingredient name", text: $item.name)
                            .font(.cavnarHeadline(17, weight: .semiBold))
                            .foregroundStyle(Color.cavnarInk)
                            .lineLimit(1)
                            .focused(focusedField, equals: .name(item.id))
                        // The industry-standard "this is editable" cue —
                        // the underline alone still reads as decorative to
                        // a first-time user; a pencil next to the name is
                        // the unambiguous signal.
                        Image(systemName: "pencil")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Color.cavnarInk.opacity(0.45))
                    }
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
