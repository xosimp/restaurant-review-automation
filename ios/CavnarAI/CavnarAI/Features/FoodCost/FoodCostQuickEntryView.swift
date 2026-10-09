import SwiftUI

/// Analytics leads (density #7): Food Cost opens on food cost % against
/// target, the 3-second answer, not on a price-entry form. The Tracker is
/// one tap (or one swipe) away, and every action sheet sits above both.
private enum FoodCostSubTab: String, CaseIterable, Identifiable {
    case analytics = "Analytics"
    case tracker = "Tracker"
    var id: String { rawValue }
}

struct FoodCostQuickEntryView: View {
    @Environment(SessionStore.self) private var sessionStore
    @State private var viewModel = FoodCostQuickEntryViewModel()
    @State private var analyticsViewModel = FoodCostAnalyticsViewModel()
    /// Orders sent and not yet received — the action row's Receive chip.
    @State private var deliveries = DeliveriesViewModel()
    @State private var subTab: FoodCostSubTab = .analytics
    @State private var showingTrackerHelp = false
    /// The action row's sheet (Friction audit #28).
    @State private var actionSheet: FoodCostAction?
    /// Where the link that pushed this screen pointed inside Food Cost —
    /// "inventory/invoices?scan=camera", "inventory/order", "invoice/12"
    /// (ModuleRoute.navPath). Opened once, on first appear (F3-7).
    var focus: NavPath? = nil
    @State private var focusSpent = false
    /// The tracker's save, waiting out its Undo window.
    @State private var pendingSave: Task<Void, Never>?
    @FocusState private var trackerFocus: TrackerField?

    /// How long Save waits before it sends, so Undo can stop it. The save
    /// overwrites this week's prices, which the 3-second press-and-hold
    /// used to guard; a plain tap with a short Undo is the iPhone way.
    private static let undoSeconds: UInt64 = 4

    init(focus: NavPath? = nil) {
        self.focus = focus
    }

    private func openSheet(_ action: FoodCostAction) {
        // The pars live on Analytics: brought into view, not a sheet.
        if case .pars = action {
            subTab = .analytics
            analyticsViewModel.scrollToPars = true
            return
        }
        actionSheet = action
    }

    var body: some View {
        // No NavigationStack of its own — pushed inside Home's or the
        // Modules tab's stack now, not a tab root.
        VStack(spacing: 0) {
            CavnarSegmentedControl(selection: $subTab, options: FoodCostSubTab.allCases) { $0.rawValue }
                .padding(.horizontal, CavnarSpace.m)
                .padding(.top, CavnarSpace.xs)
                .padding(.bottom, CavnarSpace.s)
                .cavnarRibbonHeaderAnchor()

            // The daily jobs first, on both sub-tabs — they used to sit
            // under ~10 charts on Analytics (Friction audit #28, U3-7).
            FoodCostActionRow(receiveCount: deliveries.waiting.count, open: { openSheet($0) })
                .padding(.horizontal, CavnarSpace.m)
                .padding(.bottom, CavnarSpace.s)

            if subTab == .tracker {
                tracker
            } else {
                FoodCostAnalyticsSection(viewModel: analyticsViewModel, open: { openSheet($0) })
            }
        }
        .cavnarModuleBackground()
        .navigationTitle("Food Cost")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Food Cost") }
        // Replaces the plain cavnarEmberBackButton() — swipe left anywhere
        // jumps to Tracker; swipe right or tap back while on Tracker
        // returns to Analytics first, and only leaves the module once
        // already there. See the modifier's own doc comment for why this
        // needs to own the back chevron/gesture too, not just add a swipe.
        .cavnarTabSwipeNavigation($subTab, primaryTab: .analytics, secondaryTab: .tracker)
        // Mimics Labor's Overview hero forecast pill exactly (same shared
        // DesignSystem/HeroForecastRibbon.swift component) — straddles the
        // Analytics hero card's bottom edge (see FoodCostAnalyticsSection's
        // .cavnarRibbonHeroAnchor()), only appears once that hero is on
        // screen since the anchor itself is only present then.
        .cavnarHeroForecastRibbon(
            isExpanded: $analyticsViewModel.forecastExpanded,
            tone: Color.cavnarEmber,
            icon: "calendar"
        ) {
            CavnarForecastPanel(
                title: "Food cost forecast", tone: Color.cavnarEmber, icon: "calendar",
                isExpanded: $analyticsViewModel.forecastExpanded
            ) {
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    Text(analyticsViewModel.analytics?.insight?.forecast ?? "No forecast yet — check back once this week's numbers are in.")
                        .cavnarText(.secondary)
                    // How the waste forecasts have held up here (K8) — so
                    // the owner can weigh this one, with its lean; when the
                    // record withholds the next forecast, the server's why.
                    if let record = analyticsViewModel.cfo?.brief?.forecastAccuracy?.line {
                        CavnarMixedText(record + ".", role: .caption)
                    }
                }
            }
        }
        .sheet(item: $actionSheet, onDismiss: {
            // A send, a receive or a count can change what is waiting.
            Task { await deliveries.load() }
        }) { action in
            switch action {
            case .scan(let camera): InvoiceScanSheet(startWithCamera: camera)
            case .invoice(let id): InvoiceScanSheet(invoiceId: id)
            case .count: CountSheetView()
            case .waste: WasteLogSheet()
            case .order: SupplierOrderSheet()
            case .recipes: RecipeDraftsSheet()
            case .margins: MenuMarginsSheet()
            case .menu(let dish): MenuMarginsSheet(focusDish: dish)
            case .dishes: DishScorecardSheet()
            case .suppliers: SupplierOverviewSheet()
            case .receive: ReceiveDeliveriesSheet(viewModel: deliveries)
            case .pars: EmptyView()
            }
        }
        // "inventory/invoices", "inventory/order", "inventory/count" — from
        // a push, a notification row, a Home card, a quick action, a
        // shortcut or the command sheet: all of them arrive as this screen's
        // route. Presented a beat after the push lands — a sheet asked for
        // mid-push is dropped by SwiftUI.
        .task {
            guard !focusSpent, let focus, let action = FoodCostAction(path: focus) else { return }
            focusSpent = true
            try? await Task.sleep(for: .milliseconds(450))
            openSheet(action)
        }
        .task {
            if let restaurantId = sessionStore.currentUser?.restaurantId {
                analyticsViewModel.configureCaching(restaurantId: restaurantId)
            }
        }
        // The Tracker's rows: this week's saved prices, the pantry, or the
        // defaults plus custom items — the same rows the web opens on.
        .task { await viewModel.load() }
        // What is waiting at the back door — the Receive chip.
        .task { await deliveries.load() }
        // Analytics loads the first time its tab is shown, not on opening
        // Food Cost: loading it records its recommendations (the read's
        // lines, the diagnosis, the reprice cards) as shown, and on the
        // Tracker tab none of them is on screen. An unstructured Task, so
        // flicking back to Tracker mid-load does not cancel it.
        .onChange(of: subTab, initial: true) { _, tab in
            guard tab == .analytics else { return }
            Task { await analyticsViewModel.loadOnFirstShow() }
        }
        // Reopening the app after a while re-reads the analytics once
        // they have been opened, rather than showing an earlier read as
        // current (audit 4.2). The Tracker tab is a form — nothing to reload.
        .refreshOnForeground(lastLoaded: analyticsViewModel.lastLoadedAt) {
            await analyticsViewModel.load()
        }
    }

    // MARK: - Tracker

    /// A plain list of this week's key ingredient prices (iOS readability
    /// round, 10/8/26): it was a fixed-height three-card carousel inside
    /// the page's own scroll, padded 300pt at the bottom for the keyboard,
    /// with a 3-second press-and-hold to submit.
    private static let resultAnchor = "tracker-result"

    private var tracker: some View {
        ScrollViewReader { proxy in
        ScrollView {
            VStack(alignment: .leading, spacing: CavnarSpace.l) {
                trackerIntro

                VStack(spacing: 0) {
                    ForEach($viewModel.items) { $item in
                        if item.id != viewModel.items.first?.id { AccountRowDivider() }
                        TrackerRow(item: $item, readOnly: viewModel.isReadOnly, focus: $trackerFocus) {
                            Haptic.selection()
                            let removed = item
                            withAnimation(.easeOut(duration: 0.22)) { viewModel.remove(removed) }
                        }
                    }
                }
                .cavnarCard()

                if !viewModel.isReadOnly {
                    Button {
                        Haptic.light()
                        let newItem = viewModel.addCustomRow()
                        // The new row exists on the next render pass.
                        DispatchQueue.main.async { trackerFocus = .name(newItem.id) }
                    } label: {
                        HStack(spacing: CavnarSpace.xxs) {
                            Image(systemName: "plus.circle.fill").accessibilityHidden(true)
                            Text("Add ingredient")
                        }
                        .cavnarText(.label, color: .cavnarEmber2)
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }

                if let error = viewModel.errorMessage {
                    Text(error).cavnarText(.body, color: .cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }

                // Named, not dropped. A row with no price used to be sent as
                // $0.00 and stored as a real price of record.
                if !viewModel.rowsMissingAPrice.isEmpty {
                    Text(viewModel.rowsMissingAPrice.count == 1
                         ? "\(viewModel.rowsMissingAPrice[0]) has no price yet — it won't be saved."
                         : "\(viewModel.rowsMissingAPrice.count) rows have no price yet and won't be saved.")
                        .cavnarText(.secondary, color: .cavnarAmber)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }

                if viewModel.didSubmit {
                    VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                        CavnarKicker("This week\u{2019}s prices saved", tint: .cavnarGreen)
                        resultSummary
                    }
                    .cavnarCard()
                    .id(Self.resultAnchor)
                }
            }
            .padding(CavnarSpace.gutter)
        }
        // The save's one confirmation, brought into view as it lands.
        .onChange(of: viewModel.didSubmit) { _, saved in
            guard saved else { return }
            withAnimation(.cavnarEase(0.4)) { proxy.scrollTo(Self.resultAnchor, anchor: .bottom) }
        }
        }
        .scrollDismissesKeyboard(.interactively)
        .toolbar {
            cavnarKeyboardTrailing {
                keyboardIconButton(systemName: "checkmark", enabled: true) { trackerFocus = nil }
            }
        }
        // A ledger account submits only after Edit prices, as the web hides
        // its submit until then.
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if showsSaveBar {
                CavnarPinnedBar { saveBar }
            }
        }
    }

    private var trackerIntro: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(spacing: CavnarSpace.xxs) {
                Text("This week\u{2019}s prices").cavnarText(.headline)
                Button {
                    Haptic.light()
                    showingTrackerHelp = true
                } label: {
                    Image(systemName: "questionmark.circle")
                        .font(.cavnar(.body))
                        .foregroundStyle(Color.cavnarInk2)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityLabel("How this works")
                .popover(isPresented: $showingTrackerHelp) {
                    Text("Fill in each price per unit right after an invoice arrives. Your top 8\u{2013}10 highest-cost ingredients are enough to catch a real supplier swing without turning this into busywork.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(width: 280)
                        .padding(CavnarSpace.m)
                        .presentationCompactAdaptation(.popover)
                }
            }
            // Where these rows came from, as the web's tracker says it: the
            // inventory system, the ledger, or the last typed submission.
            if let source = viewModel.source, source.synced {
                CavnarMixedText(source.line("Prices") + ". Change them there.", role: .secondary)
            } else if viewModel.fromPantry {
                Text("Prices update from scanned invoices.")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if !viewModel.isEditing {
                    Button {
                        Haptic.light()
                        viewModel.isEditing = true
                    } label: {
                        Text("Override a price").cavnarText(.label, color: .cavnarEmber2).cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
                if let typed = viewModel.typedAt {
                    CavnarMixedText("Last typed \(CavnarDate.mdy(String(typed.prefix(10))))", role: .caption)
                }
            } else {
                CavnarMixedText("Your top 8\u{2013}10 ingredients, per unit.", role: .secondary)
                if let submitted = viewModel.submittedAt {
                    CavnarMixedText("Last saved \(CavnarDate.mdy(String(submitted.prefix(10))))", role: .caption)
                }
            }
        }
    }

    /// The bar goes once the save has landed: the result card is the one
    /// confirmation (re-audit F9 — a toast, a "Saved" bar and the card used
    /// to say it three times).
    private var showsSaveBar: Bool {
        !viewModel.isReadOnly && !(viewModel.didSubmit && pendingSave == nil && !viewModel.isSubmitting)
    }

    /// Save, then a short Undo window before it sends.
    @ViewBuilder
    private var saveBar: some View {
        if pendingSave != nil || viewModel.isSubmitting {
            HStack(spacing: CavnarSpace.s) {
                if viewModel.isSubmitting {
                    CavnarShimmerText(text: "Saving\u{2026}", color: Color.cavnarInk)
                } else {
                    Text("Saving this week\u{2019}s prices\u{2026}").cavnarText(.label)
                }
                Spacer(minLength: CavnarSpace.xs)
                if pendingSave != nil && !viewModel.isSubmitting {
                    Button {
                        Haptic.light()
                        pendingSave?.cancel()
                        pendingSave = nil
                    } label: {
                        Text("Undo").frame(minWidth: 88)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
            }
            .frame(minHeight: 44)
        } else {
            Button {
                Haptic.light()
                trackerFocus = nil
                startSave()
            } label: {
                Text("Save this week\u{2019}s prices").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !viewModel.canSubmit))
            .disabled(!viewModel.canSubmit)
        }
    }

    private func startSave() {
        pendingSave = Task { @MainActor in
            try? await Task.sleep(nanoseconds: Self.undoSeconds * 1_000_000_000)
            guard !Task.isCancelled else { return }
            await viewModel.submit()
            pendingSave = nil
            guard viewModel.didSubmit else { return }
            Haptic.success()
        }
    }

    @ViewBuilder
    private var resultSummary: some View {
        if viewModel.drift.isEmpty {
            Label("Prices stable vs. last submission", systemImage: "checkmark.circle.fill")
                .cavnarText(.body, color: .cavnarGreen)
        } else {
            ForEach(viewModel.drift) { drift in
                HStack {
                    Text(drift.name).cavnarText(.label)
                    Spacer()
                    Text(String(format: "%@%.1f%%", drift.direction == "up" ? "↑ " : "↓ ", abs(drift.pctChange)))
                        .cavnarText(.figureS, color: drift.direction == "up" ? Color.cavnarRedText : Color.cavnarGreen)
                }
            }
            if let total = viewModel.totalWeeklyImpact, total > 0 {
                CavnarMixedText("Est. +$\(Int(total))/week from price increases", role: .label, color: .cavnarAmber)
            }
        }
    }
}

/// Which field, on which row, has focus — owned by the tracker so "Add
/// ingredient" can move focus onto a new row's name.
private enum TrackerField: Hashable {
    case name(UUID), unit(UUID), price(UUID), usage(UUID)
}

/// One ingredient's row: the name and unit, then this week's price and
/// how much is used a week. Read-only rows (a ledger account before Edit
/// prices, a synced account) take no input.
private struct TrackerRow: View {
    @Binding var item: FoodCostItem
    var readOnly: Bool
    var focus: FocusState<TrackerField?>.Binding
    var onDelete: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                TextField("Ingredient name", text: $item.name)
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarInk)
                    .focused(focus, equals: .name(item.id))
                TextField("unit", text: $item.unit)
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarInk2)
                    .multilineTextAlignment(.trailing)
                    .frame(width: 56)
                    .focused(focus, equals: .unit(item.id))
                if !readOnly {
                    Button(action: onDelete) {
                        Image(systemName: "xmark.circle")
                            .font(.cavnar(.body))
                            .foregroundStyle(Color.cavnarInk2)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Remove \(item.name.isEmpty ? "this row" : item.name)")
                }
            }
            HStack(spacing: CavnarSpace.m) {
                field("Price", prefix: "$", text: $item.priceText, field: .price(item.id))
                field("Used a week", prefix: nil, text: $item.usageText, field: .usage(item.id))
            }
        }
        .padding(.vertical, CavnarSpace.s)
        .disabled(readOnly)
    }

    private func field(_ label: String, prefix: String?, text: Binding<String>, field: TrackerField) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).cavnarText(.caption)
            HStack(spacing: 2) {
                if let prefix {
                    Text(prefix).cavnarText(.figureS, color: .cavnarInk2)
                }
                TextField("0", text: text)
                    .keyboardType(.decimalPad)
                    .font(.cavnar(.figureS))
                    .foregroundStyle(Color.cavnarInk)
                    .focused(focus, equals: field)
            }
            .padding(.horizontal, CavnarSpace.xs)
            .frame(minHeight: 44)
            .background(Color.cavnarPaper3.opacity(readOnly ? 0.15 : 0.35),
                        in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Orders waiting to arrive, each received in place — the action row's
/// Receive chip opens this directly (iOS readability round, 10/8/26: it
/// lived only at the bottom of Send order). The same view model as the
/// chip, so the count it shows falls as each order comes in.
struct ReceiveDeliveriesSheet: View {
    let viewModel: DeliveriesViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                DeliveriesSection(viewModel: viewModel, title: "Waiting to arrive", waitingOnly: true)
                    .padding(CavnarSpace.gutter)
            }
            .scrollDismissesKeyboard(.immediately)
            .cavnarModuleBackground()
            .navigationTitle("Receive")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Receive")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        dismiss()
                    } label: {
                        Text("Done").cavnarText(.label, color: .cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .cavnarEmberRefreshable { await viewModel.load() }
            .task { await viewModel.load() }
        }
    }
}
