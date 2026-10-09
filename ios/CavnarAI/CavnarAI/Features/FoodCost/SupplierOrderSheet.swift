import SwiftUI

/// The step the Food Cost order list used to stop short of: the computed
/// quantities, grouped by the supplier who actually fills them, with a way
/// to send each order and close it out when it arrives.
///
/// Every quantity can be changed before sending, and every send is
/// confirmed with the supplier, the line count and the total — one tap
/// used to email a supplier (the web has always asked first).
struct SupplierOrderSheet: View {
    @State private var viewModel = SupplierOrderViewModel()
    @State private var deliveries = DeliveriesViewModel()
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focusedLine: String?
    /// A followed game this week and what its kind used here (#78).
    @State private var game: FoodCostGameWeek.Game?
    /// The inventory system's sync — its suppliers are then read-only (#8).
    @State private var source: InventorySyncSource?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    if viewModel.isLoading && viewModel.draft == nil {
                        CavnarSkeletonLines(widths: [1.0, 0.86, 0.7, 0.55, 0.4])
                    } else if let error = viewModel.errorMessage, viewModel.draft == nil {
                        Text(error)
                            .font(.cavnar(.body))
                            .foregroundStyle(Color.cavnarInk2)
                    } else if let draft = viewModel.draft {
                        // A refused send (the draft changed, or it already
                        // went) — shown above the reloaded draft.
                        if let error = viewModel.errorMessage {
                            Text(error)
                                .cavnarText(.secondary, color: .cavnarRedText)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if let result = viewModel.lastResult {
                            resultBanner(result)
                        }
                        if let game {
                            GameWeekCard(game: game)
                        }
                        // Deliveries waiting to arrive lead: receiving is
                        // the job at the back door, and it sat under every
                        // supplier card (iOS readability round, 10/8/26).
                        if !deliveries.waiting.isEmpty {
                            DeliveriesSection(viewModel: deliveries, title: "Waiting to arrive", waitingOnly: true)
                        }
                        if draft.isEmpty {
                            emptyState
                        } else {
                            ForEach(draft.groups) { group in
                                supplierCard(group, pinned: draft.groups.count == 1)
                            }
                            if !draft.unassigned.isEmpty {
                                unassignedCard(draft.unassigned)
                            }
                        }
                        // Received and earlier orders, under the drafts.
                        if deliveries.orders.contains(where: { $0.isReceived }) || deliveries.loadError != nil {
                            DeliveriesSection(viewModel: deliveries, title: "Recent orders", receivedOnly: true)
                        }
                    }
                }
                .padding(CavnarSpace.gutter)
            }
            // The send in thumb reach (iOS readability round): one supplier's
            // "Send to …", or "Send all" when there are several — each still
            // only asks; the confirmation sends.
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if let draft = viewModel.draft, !draft.isEmpty {
                    if draft.groups.count == 1, let group = draft.groups.first {
                        CavnarPinnedBar { sendButton(group) }
                    } else if draft.groups.count > 1 {
                        CavnarPinnedBar { sendAllButton(draft) }
                    }
                }
            }
            .cavnarModuleBackground()
            .navigationTitle("Send order")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Send order")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        dismiss()
                    } label: {
                        Text("Done").font(.cavnar(.label)).foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .scrollDismissesKeyboard(.immediately)
            .task {
                async let draft: Void = viewModel.load()
                async let orders: Void = deliveries.load()
                async let week: FoodCostGameWeek? = try? APIClient.shared.send("/mobile/api/food-cost/game-week",
                                                                              hapticOnError: false)
                async let sheet: CountSheetSourceOnly? = try? APIClient.shared.send("/mobile/api/food-cost/count-sheet",
                                                                                    hapticOnError: false)
                _ = await (draft, orders)
                game = (await week)?.game
                source = (await sheet)?.source
            }
            // A send just made shows up in the orders below.
            .onChange(of: viewModel.lastResult?.sent.count) { _, _ in
                Task { await deliveries.load() }
            }
            .sheet(item: $viewModel.assigningItem) { item in
                SupplierAssignSheet(viewModel: viewModel, item: item)
            }
            // An email to a supplier leaves the restaurant: a confirmation
            // dialog naming who, how many lines and how much (DESIGN_SYSTEM
            // "Post to all connected" is the same rule).
            .confirmationDialog(
                viewModel.pendingSend?.resend == true ? "Already sent" : "Send this order?",
                isPresented: Binding(
                    get: { viewModel.pendingSend != nil },
                    set: { if !$0 { viewModel.pendingSend = nil } }),
                titleVisibility: .visible,
                presenting: viewModel.pendingSend
            ) { pending in
                Button(pending.resend ? "Send it again" : "Send it") {
                    Haptic.light()
                    Task { await viewModel.confirmSend(pending) }
                }
                Button(pending.resend ? "Leave it" : "Not yet", role: .cancel) {}
            } message: { pending in
                Text(viewModel.confirmMessage(pending))
            }
        }
    }

    // MARK: - Draft

    /// One supplier's order. `pinned`: the only supplier, whose Send is in
    /// the pinned bar; with several, each card keeps its own (secondary)
    /// Send and the pinned bar sends them all.
    private func supplierCard(_ group: SupplierOrderGroup, pinned: Bool) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            // The name alone — the email is in the send confirmation.
            Text(group.displayName).cavnarText(.lead)

            VStack(spacing: 0) {
                ForEach(Array(group.items.enumerated()), id: \.element.id) { index, item in
                    itemRow(item, group: group)
                    if index < group.items.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }

            HStack {
                Text("Estimated").cavnarText(.secondary)
                Spacer()
                // The total at the owner's quantities, not the draft's.
                Text(SupplierOrderViewModel.money(viewModel.summary(group).total))
                    .cavnarText(.figureS)
                    .cavnarSensitive()
            }

            if !pinned {
                sendButton(group, primary: false)
            }
        }
        .cavnarCard()
    }

    /// Asks to send one supplier's order — the confirmation sends it.
    private func sendButton(_ group: SupplierOrderGroup, primary: Bool = true) -> some View {
        Button {
            Haptic.light()
            focusedLine = nil
            viewModel.askToSend(group)
        } label: {
            Group {
                if viewModel.isSending {
                    CavnarShimmerText(text: "Sending…")
                } else {
                    Text("Send to \(group.displayName)")
                }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(SendButtonStyle(primary: primary, isDisabled: viewModel.isSending))
        .disabled(viewModel.isSending)
    }

    /// Primary in the pinned bar; secondary on a card when the bar sends all.
    private struct SendButtonStyle: ButtonStyle {
        let primary: Bool
        let isDisabled: Bool
        @ViewBuilder
        func makeBody(configuration: Configuration) -> some View {
            if primary {
                CavnarPrimaryButtonStyle(isDisabled: isDisabled).makeBody(configuration: configuration)
            } else {
                CavnarSecondaryButtonStyle(isDisabled: isDisabled).makeBody(configuration: configuration)
            }
        }
    }

    /// A line with its quantity open to change (0 takes it off the order).
    private func itemRow(_ item: SupplierOrderItem, group: SupplierOrderGroup) -> some View {
        let key = group.supplierEmail + "|" + item.lineKey
        let valid = viewModel.quantity(group, item) != nil
        return HStack(alignment: .center, spacing: 10) {
            Rectangle()
                .fill(item.isCritical ? Color.cavnarRed : Color.cavnarAmber)
                .frame(width: 3, height: 16)
                .clipShape(Capsule())
            VStack(alignment: .leading, spacing: 2) {
                Text(item.item)
                    .font(.cavnar(.body))
                    .foregroundStyle(Color.cavnarInk)
                if item.trimmedForWaste == true {
                    Text("Trimmed for last week\u{2019}s waste")
                        .cavnarText(.caption, color: .cavnarInk2)
                }
                // The owner's own ordering habit applied (memory round).
                if let adjusted = item.adjustmentLine {
                    HomeMixedText.make(adjusted, role: .caption, color: .cavnarEmber2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 8)
            TextField("0", text: Binding(
                get: { viewModel.quantityText(group, item) },
                set: { viewModel.setQuantity($0, group: group, item: item) }))
                .keyboardType(.decimalPad)
                .multilineTextAlignment(.trailing)
                .font(.cavnar(.figureS))
                .foregroundStyle(valid ? Color.cavnarInk : Color.cavnarRedText)
                .frame(width: 64, height: 44)
                .padding(.horizontal, 8)
                .background(RoundedRectangle(cornerRadius: 8).stroke(valid ? Color.cavnarPaper3 : Color.cavnarRed, lineWidth: 1))
                .focused($focusedLine, equals: key)
                .accessibilityLabel("\(item.item) quantity")
            Text(item.unit)
                .font(.cavnar(.secondary))
                .foregroundStyle(Color.cavnarInk2)
                .frame(minWidth: 28, alignment: .leading)
        }
        .padding(.vertical, 7)
    }

    private func unassignedCard(_ items: [SupplierOrderItem]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            CavnarKicker("No supplier yet", tint: .cavnarAmber)
            if let source, source.synced {
                // Synced: suppliers are set in the inventory system (#8).
                HomeMixedText.make("These are on the order list but have nowhere to go. "
                                   + source.line("Suppliers") + " \u{2014} set theirs there.", role: .secondary, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("These are on the order list but have nowhere to go. Add a supplier and they'll be included next time.")
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    Button {
                        Haptic.light()
                        guard source?.synced != true else { return }
                        viewModel.assigningItem = item
                    } label: {
                        HStack(spacing: 10) {
                            Text(item.item)
                                .font(.cavnar(.body))
                                .foregroundStyle(Color.cavnarInk)
                            Spacer(minLength: 8)
                            (Text(SupplierOrderItem.qtyString(item.qty)).font(.cavnar(.figureS))
                                + Text(item.unit.isEmpty ? "" : " \(item.unit)").font(.cavnar(.secondary)))
                                .foregroundStyle(Color.cavnarInk2)
                            Image(systemName: "chevron.right")
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                        .padding(.vertical, 11)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    if index < items.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
        .cavnarCard()
    }

    /// Confirmed like one supplier's send, naming every supplier, the line
    /// count and the total; each order then goes with its own quantities.
    private func sendAllButton(_ draft: SupplierOrderDraft) -> some View {
        Button {
            Haptic.light()
            focusedLine = nil
            viewModel.askToSendAll()
        } label: {
            Group {
                if viewModel.isSending {
                    CavnarShimmerText(text: "Sending…")
                } else {
                    Text("Send all \(draft.groups.count) orders")
                }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSending))
        .disabled(viewModel.isSending)
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Nothing to order")
                .font(.cavnar(.lead))
                .foregroundStyle(Color.cavnarInk)
            Text("Everything is above par right now. This fills in as stock runs down.")
                .font(.cavnar(.secondary))
                .foregroundStyle(Color.cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .cavnarCard()
    }

    // MARK: - Outcome

    private func resultBanner(_ result: SendOrderResult) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            // The owner's send delay queued it rather than sending it.
            if let minutes = result.undoMinutes, result.sent.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "clock")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarEmber2)
                    (Text("Goes out in ").font(.cavnar(.secondary))
                        + Text("\(minutes)").font(.cavnar(.figureS))
                        + Text(" min — undo from Home").font(.cavnar(.secondary)))
                        .foregroundStyle(Color.cavnarInk)
                }
            }
            ForEach(result.sent) { sent in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarGreen)
                    (Text("\(sent.poNumber) ").font(.cavnar(.figureS))
                        + Text("sent to \(sent.supplierName)").font(.cavnar(.secondary)))
                        .foregroundStyle(Color.cavnarInk)
                }
            }
            ForEach(result.failed) { failure in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarRed)
                    // The supplier by name and a plain sentence, not an
                    // address and the server's error (re-audit F10).
                    Text(Self.failureLine(supplier: supplierName(for: failure.supplierEmail)))
                        .cavnarText(.secondary, color: .cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    /// The draft's name for a supplier's order address; the address only
    /// when the draft no longer lists it.
    private func supplierName(for email: String) -> String {
        viewModel.draft?.groups.first { $0.supplierEmail.lowercased() == email.lowercased() }?.displayName ?? email
    }

    /// "The order to Sysco didn’t go out. Check the draft and send it again."
    static func failureLine(supplier: String) -> String {
        "The order to \(supplier) didn\u{2019}t go out. Check the draft and send it again."
    }

    private static func currency(_ value: Double) -> String {
        "$\(Int(value.rounded()).formatted())"
    }
}

/// Assigns the supplier an ingredient is ordered from — the one piece of
/// data the order list needed before it could be sent anywhere.
private struct SupplierAssignSheet: View {
    let viewModel: SupplierOrderViewModel
    let item: SupplierOrderItem

    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var email = ""
    @FocusState private var focusedField: Field?

    private enum Field: Hashable, CaseIterable { case name, email }

    private var canSubmit: Bool {
        !viewModel.isSavingSupplier && email.contains("@")
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    Text("Where do you order \(item.item) from? Orders for every ingredient from the same supplier are sent together as one order.")
                        .font(.cavnar(.body))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)

                    CavnarFloatingField(
                        icon: "building.2", placeholder: "Supplier name", text: $name,
                        focus: $focusedField, field: .name
                    )
                    CavnarFloatingField(
                        icon: "envelope", placeholder: "Supplier email", text: $email,
                        autocapitalization: .never, focus: $focusedField, field: .email
                    )

                    if let error = viewModel.supplierError {
                        Text(error).font(.cavnar(.body)).foregroundStyle(Color.cavnarRedText)
                    }

                    VStack(spacing: 10) {
                        Button {
                            Task {
                                if await viewModel.assignSupplier(to: item.item, name: name, email: email) {
                                    dismiss()
                                }
                            }
                        } label: {
                            Group {
                                if viewModel.isSavingSupplier {
                                    CavnarShimmerText(text: "Saving…")
                                } else {
                                    Text("Save supplier")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit))
                        .disabled(!canSubmit)

                        Button { dismiss() } label: {
                            Text("Cancel").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Supplier")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Supplier") }
            .keyboardNavToolbar($focusedField)
        }
    }
}

/// Just the count sheet's `source` — the order sheet asks whether the
/// suppliers are an inventory system's.
struct CountSheetSourceOnly: Decodable {
    let source: InventorySyncSource?
}
