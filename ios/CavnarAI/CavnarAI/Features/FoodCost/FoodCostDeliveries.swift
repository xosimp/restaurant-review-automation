import SwiftUI
import Observation

/// Receiving deliveries into stock (friction audit U2-12) — the phone's
/// half of templates/_fc_receive_js.html. "Received as ordered" puts every
/// line into stock; "Some were short" opens the lines to change what
/// arrived first, so a short delivery is recorded as short rather than as
/// the full order. Failures are said, not swallowed: "Mark received" used
/// to post no lines and drop any error, so an order simply stayed open
/// with no word why.
///
/// Shared by Send order and the counts-only Food Cost screen; for a login
/// without FOOD_COST_VIEW the server takes every cost off the orders, and
/// nothing here shows one.
@Observable
@MainActor
final class DeliveriesViewModel {
    var orders: [PurchaseOrder] = []
    var isLoading = false
    var loadError: String?
    /// The order whose lines are open for a short delivery.
    var shortOpen: Int?
    /// order id -> line key -> the quantity that arrived, as typed.
    var arrived: [Int: [String: String]] = [:]
    var receiving: Set<Int> = []
    /// order id -> why the receive failed, shown on that order.
    var errors: [Int: String] = [:]
    /// The last receive's result, in a sentence.
    var receivedLine: String?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    private struct OrdersResponse: Decodable { let ok: Bool; let orders: [PurchaseOrder] }

    var waiting: [PurchaseOrder] { orders.filter { !$0.isReceived } }

    func load() async {
        isLoading = orders.isEmpty
        defer { isLoading = false }
        do {
            let r: OrdersResponse = try await client.send("/mobile/api/food-cost/purchase-orders", hapticOnError: false)
            orders = r.orders
            loadError = nil
        } catch let error as APIClient.APIError {
            if orders.isEmpty { loadError = error.message }
        } catch is CancellationError {
        } catch {
            if orders.isEmpty { loadError = "Couldn\u{2019}t load your deliveries." }
        }
    }

    func toggleShort(_ order: PurchaseOrder) {
        if shortOpen == order.id { shortOpen = nil; return }
        shortOpen = order.id
        if arrived[order.id] == nil {
            arrived[order.id] = Dictionary(order.items.map { ($0.lineKey, SupplierOrderItem.qtyString($0.qty)) },
                                           uniquingKeysWith: { a, _ in a })
        }
    }

    func arrivedText(_ order: PurchaseOrder, _ item: SupplierOrderItem) -> String {
        arrived[order.id]?[item.lineKey] ?? SupplierOrderItem.qtyString(item.qty)
    }

    func setArrived(_ text: String, order: PurchaseOrder, item: SupplierOrderItem) {
        arrived[order.id, default: [:]][item.lineKey] = text
    }

    private struct ReceiveBody: Encodable {
        struct Line: Encodable {
            let ingredientId: Int?
            let item: String?
            let qty: Double
            enum CodingKeys: String, CodingKey { case item, qty; case ingredientId = "ingredient_id" }
        }
        let lines: [Line]?
    }

    private struct ReceiveResponse: Decodable {
        struct Posted: Decodable { let item: String }
        struct Short: Decodable { let item: String }
        let ok: Bool
        let poNumber: String?
        let posted: [Posted]?
        let short: [Short]?
        let skipped: [String]?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, posted, short, skipped, error
            case poNumber = "po_number"
        }
    }

    /// `withLines`: the quantities typed under "Some were short"; without
    /// them every line is received as ordered.
    func receive(_ order: PurchaseOrder, withLines: Bool) async {
        guard !receiving.contains(order.id) else { return }
        errors[order.id] = nil
        receivedLine = nil
        var lines: [ReceiveBody.Line]?
        if withLines {
            var out: [ReceiveBody.Line] = []
            for item in order.items {
                let text = arrivedText(order, item).trimmingCharacters(in: .whitespaces)
                guard let q = FoodCostQuickEntryViewModel.parsedPrice(text), q >= 0 else {
                    errors[order.id] = "Quantities are numbers, 0 or more."
                    return
                }
                out.append(.init(ingredientId: item.ingredientId,
                                 item: item.ingredientId == nil ? item.item : nil, qty: q))
            }
            lines = out
        }
        receiving.insert(order.id)
        defer { receiving.remove(order.id) }
        do {
            let r: ReceiveResponse = try await client.send(
                "/mobile/api/food-cost/purchase-orders/\(order.id)/received", method: .post,
                body: ReceiveBody(lines: lines), retryTransient: false)
            guard r.ok else {
                errors[order.id] = r.error ?? "Couldn\u{2019}t receive that order."
                return
            }
            let n = r.posted?.count ?? 0
            let s = r.short?.count ?? 0
            let k = r.skipped?.count ?? 0
            var line = "\(r.poNumber ?? order.poNumber) received — \(n) line\(n == 1 ? "" : "s") added to stock"
            if s > 0 { line += " · \(s) short" }
            if k > 0 { line += " · \(k) not on your ingredient list" }
            receivedLine = line + "."
            shortOpen = nil
            arrived[order.id] = nil
            await Haptic.success()
            await load()
        } catch let error as APIClient.APIError {
            errors[order.id] = error.message
        } catch is CancellationError {
        } catch {
            errors[order.id] = "Couldn\u{2019}t receive that order."
        }
    }
}

/// The orders list with receiving on each open one.
struct DeliveriesSection: View {
    let viewModel: DeliveriesViewModel
    /// Counts-only: only what is waiting to arrive, and never a cost.
    var showMoney = true
    var title = "RECENT ORDERS"

    var body: some View {
        let shown = showMoney ? viewModel.orders : viewModel.waiting
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            if let line = viewModel.receivedLine {
                HomeMixedText.make(line, size: 14, weight: 600, color: .cavnarGreen)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = viewModel.loadError {
                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
            } else if shown.isEmpty {
                Text("Nothing waiting to arrive.")
                    .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
            }
            VStack(spacing: 0) {
                ForEach(Array(shown.enumerated()), id: \.element.id) { index, order in
                    orderRow(order)
                    if index < shown.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
        .cavnarCard()
    }

    private func orderRow(_ order: PurchaseOrder) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    (Text(order.poNumber).font(.cavnarNumber(14.5, weight: 700))
                        + Text(" · \(order.supplierName.isEmpty ? order.supplierEmail : order.supplierName)").font(.cavnarBody(14.5)))
                        .foregroundStyle(Color.cavnarInk)
                    HomeMixedText.make(detail(order), size: 13.5, weight: 500, color: .cavnarInk3)
                        .cavnarSensitive()
                }
                Spacer(minLength: 8)
                if order.isReceived {
                    Text("Received")
                        .font(.cavnarBody(13.5, weight: 700))
                        .foregroundStyle(Color.cavnarGreen)
                }
            }
            if !order.isReceived {
                HStack(spacing: 10) {
                    Button {
                        Haptic.light()
                        Task { await viewModel.receive(order, withLines: false) }
                    } label: {
                        Group {
                            if viewModel.receiving.contains(order.id) && viewModel.shortOpen != order.id {
                                CavnarShimmerText(text: "Receiving…")
                            } else {
                                Text("Received as ordered")
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    Button {
                        Haptic.light()
                        viewModel.toggleShort(order)
                    } label: {
                        Text("Some were short")
                            .font(.cavnarBody(14, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
                .disabled(viewModel.receiving.contains(order.id))
                if viewModel.shortOpen == order.id {
                    shortLines(order)
                }
                if let error = viewModel.errors[order.id] {
                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.vertical, 11)
    }

    private func shortLines(_ order: PurchaseOrder) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Change what arrived, then receive.")
                .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
            ForEach(order.items) { item in
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.item).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk)
                        HomeMixedText.make("Ordered \(SupplierOrderItem.qtyString(item.qty))\(item.unit.isEmpty ? "" : " \(item.unit)")",
                                           size: 12.5, weight: 500, color: .cavnarInk3)
                    }
                    Spacer(minLength: 8)
                    TextField("0", text: Binding(
                        get: { viewModel.arrivedText(order, item) },
                        set: { viewModel.setArrived($0, order: order, item: item) }))
                        .keyboardType(.decimalPad)
                        .multilineTextAlignment(.trailing)
                        .font(.cavnarNumber(15, weight: 600))
                        .frame(width: 80)
                        .padding(.vertical, 6).padding(.horizontal, 8)
                        .background(RoundedRectangle(cornerRadius: 8).stroke(Color.cavnarPaper3, lineWidth: 1))
                        .accessibilityLabel("\(item.item) arrived")
                }
            }
            Button {
                Haptic.light()
                Task { await viewModel.receive(order, withLines: true) }
            } label: {
                Group {
                    if viewModel.receiving.contains(order.id) {
                        CavnarShimmerText(text: "Receiving…")
                    } else {
                        Text("Receive these")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.receiving.contains(order.id)))
            .disabled(viewModel.receiving.contains(order.id))
        }
        .padding(.top, 4)
    }

    private func detail(_ order: PurchaseOrder) -> String {
        var parts = ["\(order.items.count) item\(order.items.count == 1 ? "" : "s")"]
        if !order.sentAt.isEmpty { parts.append("sent \(CavnarDate.mdy(String(order.sentAt.prefix(10))))") }
        if showMoney, let total = order.totalCost { parts.append("$\(Int(total.rounded()).formatted())") }
        return parts.joined(separator: " · ")
    }
}
