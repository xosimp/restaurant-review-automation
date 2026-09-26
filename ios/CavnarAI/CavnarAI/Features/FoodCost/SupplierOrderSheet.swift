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

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    if viewModel.isLoading && viewModel.draft == nil {
                        CavnarSkeletonLines(widths: [1.0, 0.86, 0.7, 0.55, 0.4])
                    } else if let error = viewModel.errorMessage, viewModel.draft == nil {
                        Text(error)
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk3)
                    } else if let draft = viewModel.draft {
                        // A refused send (the draft changed, or it already
                        // went) — shown above the reloaded draft.
                        if let error = viewModel.errorMessage {
                            Text(error)
                                .font(.cavnarBody(14))
                                .foregroundStyle(Color.cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if let result = viewModel.lastResult {
                            resultBanner(result)
                        }
                        if draft.isEmpty {
                            emptyState
                        } else {
                            ForEach(draft.groups) { group in
                                supplierCard(group)
                            }
                            if !draft.unassigned.isEmpty {
                                unassignedCard(draft.unassigned)
                            }
                            if draft.groups.count > 1 {
                                sendAllButton(draft)
                            }
                        }
                        if !deliveries.orders.isEmpty || deliveries.loadError != nil {
                            DeliveriesSection(viewModel: deliveries)
                        }
                    }
                }
                .padding(20)
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
                        Text("Done").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .scrollDismissesKeyboard(.immediately)
            .task {
                async let draft: Void = viewModel.load()
                async let orders: Void = deliveries.load()
                _ = await (draft, orders)
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

    private func supplierCard(_ group: SupplierOrderGroup) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 3) {
                Text(group.supplierName)
                    .font(.cavnarHeadline(17))
                    .foregroundStyle(Color.cavnarInk)
                Text(group.supplierEmail)
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
            }

            VStack(spacing: 0) {
                ForEach(Array(group.items.enumerated()), id: \.element.id) { index, item in
                    itemRow(item, group: group)
                    if index < group.items.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }

            HStack {
                Text("Estimated")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
                Spacer()
                // The total at the owner's quantities, not the draft's.
                Text(SupplierOrderViewModel.money(viewModel.summary(group).total))
                    .font(.cavnarNumber(15, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .cavnarSensitive()
            }

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
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSending))
            .disabled(viewModel.isSending)
        }
        .cavnarCard()
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
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk)
                if item.trimmedForWaste == true {
                    Text("trimmed for last week\u{2019}s waste")
                        .font(.cavnarBody(12.5))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer(minLength: 8)
            TextField("0", text: Binding(
                get: { viewModel.quantityText(group, item) },
                set: { viewModel.setQuantity($0, group: group, item: item) }))
                .keyboardType(.decimalPad)
                .multilineTextAlignment(.trailing)
                .font(.cavnarNumber(15, weight: 700))
                .foregroundStyle(valid ? Color.cavnarInk : Color.cavnarRed)
                .frame(width: 64)
                .padding(.vertical, 5).padding(.horizontal, 8)
                .background(RoundedRectangle(cornerRadius: 8).stroke(valid ? Color.cavnarPaper3 : Color.cavnarRed, lineWidth: 1))
                .focused($focusedLine, equals: key)
                .accessibilityLabel("\(item.item) quantity")
            Text(item.unit)
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .frame(minWidth: 28, alignment: .leading)
        }
        .padding(.vertical, 7)
    }

    private func unassignedCard(_ items: [SupplierOrderItem]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("NO SUPPLIER YET")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarAmber)
            Text("These are on the order list but have nowhere to go. Add a supplier and they'll be included next time.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    Button {
                        Haptic.light()
                        viewModel.assigningItem = item
                    } label: {
                        HStack(spacing: 10) {
                            Text(item.item)
                                .font(.cavnarBody(15))
                                .foregroundStyle(Color.cavnarInk)
                            Spacer(minLength: 8)
                            (Text(SupplierOrderItem.qtyString(item.qty)).font(.cavnarNumber(15, weight: 700))
                                + Text(item.unit.isEmpty ? "" : " \(item.unit)").font(.cavnarBody(13.5)))
                                .foregroundStyle(Color.cavnarInk3)
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
        .buttonStyle(CavnarSecondaryButtonStyle())
        .disabled(viewModel.isSending)
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Nothing to order")
                .font(.cavnarHeadline(17))
                .foregroundStyle(Color.cavnarInk)
            Text("Everything is above par right now. This fills in as stock runs down.")
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
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
                    (Text("Goes out in ").font(.cavnarBody(14))
                        + Text("\(minutes)").font(.cavnarNumber(14, weight: 700))
                        + Text(" min — undo from Home").font(.cavnarBody(14)))
                        .foregroundStyle(Color.cavnarInk)
                }
            }
            ForEach(result.sent) { sent in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarGreen)
                    (Text("\(sent.poNumber) ").font(.cavnarNumber(14, weight: 700))
                        + Text("sent to \(sent.supplierName)").font(.cavnarBody(14)))
                        .foregroundStyle(Color.cavnarInk)
                }
            }
            ForEach(result.failed) { failure in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarRed)
                    Text("\(failure.supplierEmail) — \(failure.error)")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
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
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
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
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
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
