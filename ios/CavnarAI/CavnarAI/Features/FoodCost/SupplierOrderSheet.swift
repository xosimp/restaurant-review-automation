import SwiftUI

/// The step the Food Cost order list used to stop short of: the computed
/// quantities, grouped by the supplier who actually fills them, with a way
/// to send each order and close it out when it arrives.
struct SupplierOrderSheet: View {
    @State private var viewModel = SupplierOrderViewModel()
    @Environment(\.dismiss) private var dismiss

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
                        if !viewModel.orders.isEmpty {
                            historySection
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
            .task { await viewModel.load() }
            .sheet(item: $viewModel.assigningItem) { item in
                SupplierAssignSheet(viewModel: viewModel, item: item)
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
                    itemRow(item)
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
                Text(Self.currency(group.totalCost))
                    .font(.cavnarNumber(15, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .cavnarSensitive()
            }

            Button {
                Task { await viewModel.send(supplierEmail: group.supplierEmail) }
            } label: {
                Group {
                    if viewModel.isSending {
                        CavnarShimmerText(text: "Sending…")
                    } else {
                        Text("Send to \(group.supplierName)")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSending))
            .disabled(viewModel.isSending)
        }
        .cavnarCard()
    }

    private func itemRow(_ item: SupplierOrderItem) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Rectangle()
                .fill(item.isCritical ? Color.cavnarRed : Color.cavnarAmber)
                .frame(width: 3, height: 16)
                .clipShape(Capsule())
            Text(item.item)
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk)
            Spacer(minLength: 8)
            (Text("\(item.qty)").font(.cavnarNumber(15, weight: 700))
                + Text(item.unit.isEmpty ? "" : " \(item.unit)").font(.cavnarBody(13.5)))
                .foregroundStyle(Color.cavnarInk)
        }
        .padding(.vertical, 9)
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
                            (Text("\(item.qty)").font(.cavnarNumber(15, weight: 700))
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

    private func sendAllButton(_ draft: SupplierOrderDraft) -> some View {
        Button {
            Task { await viewModel.send() }
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

    // MARK: - History

    private var historySection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("RECENT ORDERS")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 0) {
                ForEach(Array(viewModel.orders.enumerated()), id: \.element.id) { index, order in
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            (Text(order.poNumber).font(.cavnarNumber(14.5, weight: 700))
                                + Text(" · \(order.supplierName)").font(.cavnarBody(14.5)))
                                .foregroundStyle(Color.cavnarInk)
                            Text("\(order.items.count) item\(order.items.count == 1 ? "" : "s") · \(Self.currency(order.totalCost))")
                                .font(.cavnarBody(13.5))
                                .foregroundStyle(Color.cavnarInk3)
                                .cavnarSensitive()
                        }
                        Spacer(minLength: 8)
                        if order.isReceived {
                            Text("Received")
                                .font(.cavnarBody(13.5, weight: 700))
                                .foregroundStyle(Color.cavnarGreen)
                        } else {
                            Button {
                                Haptic.light()
                                Task { await viewModel.markReceived(order) }
                            } label: {
                                Text("Mark received")
                                    .font(.cavnarBody(13.5, weight: 700))
                                    .foregroundStyle(Color.cavnarEmber2)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.vertical, 11)
                    if index < viewModel.orders.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
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
