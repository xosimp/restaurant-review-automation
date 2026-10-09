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
            queued = Set(await PendingWriteQueue.shared.pending(pathPrefix: QueuedWrite.receivePathPrefix)
                .compactMap { QueuedWrite.receivePoId($0.path) })
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

    /// Every key /purchase-orders/<id>/received reads (`lines` only for a
    /// short delivery), plus the `idempotency_key` that answers a resend
    /// with the first result (parity audit #23).
    struct ReceiveBody: Encodable, Equatable {
        struct Line: Encodable, Equatable {
            let ingredientId: Int?
            let item: String?
            let qty: Double
            enum CodingKeys: String, CodingKey { case item, qty; case ingredientId = "ingredient_id" }
        }
        let lines: [Line]?
        var idempotencyKey: String? = nil
        enum CodingKeys: String, CodingKey { case lines; case idempotencyKey = "idempotency_key" }
    }

    /// One key per order until its receive lands.
    private var receiveKeys: [Int: String] = [:]
    /// Orders whose receive is parked in the offline queue — read back from
    /// the queue (refreshQueued), not only remembered: after a relaunch the
    /// order must still read "kept on this phone", or a second receive goes
    /// out and the parked one replays onto a received order (re-audit
    /// 10/8/26 #7).
    var queued: Set<Int> = []

    func refreshQueued() async {
        let writes = await PendingWriteQueue.shared.pending(pathPrefix: QueuedWrite.receivePathPrefix)
        let now = Set(writes.compactMap { QueuedWrite.receivePoId($0.path) })
        // A receive that left the queue landed (or was given up): re-read.
        let landed = !queued.subtracting(now).isEmpty
        queued = now
        if landed { await load() }
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

    // MARK: - Received as ordered, with a short Undo (re-audit F11)

    /// How long "Received as ordered" waits before it posts, so Undo can
    /// stop it: it puts every line into stock on one tap.
    static let undoSeconds: Double = 4
    /// Orders waiting out their Undo window.
    var pendingReceive: Set<Int> = []
    private var pendingTasks: [Int: Task<Void, Never>] = [:]

    func receiveAsOrdered(_ order: PurchaseOrder) {
        guard pendingTasks[order.id] == nil, !receiving.contains(order.id), !queued.contains(order.id) else { return }
        errors[order.id] = nil
        pendingReceive.insert(order.id)
        pendingTasks[order.id] = Task { [weak self] in
            try? await Task.sleep(for: .seconds(DeliveriesViewModel.undoSeconds))
            guard !Task.isCancelled, let self else { return }
            await self.commitPending(order)
        }
    }

    func undoReceive(_ order: PurchaseOrder) {
        pendingTasks[order.id]?.cancel()
        pendingTasks[order.id] = nil
        pendingReceive.remove(order.id)
    }

    private func commitPending(_ order: PurchaseOrder) async {
        pendingTasks[order.id] = nil
        guard pendingReceive.remove(order.id) != nil else { return }
        await receive(order, withLines: false)
    }

    /// The list is going off screen: a receive waiting out its Undo goes
    /// now rather than being lost with the screen.
    func commitPendingReceives() {
        let due = orders.filter { pendingReceive.contains($0.id) }
        for order in due {
            pendingTasks[order.id]?.cancel()
            pendingTasks[order.id] = nil
            pendingReceive.remove(order.id)
            Task { await self.receive(order, withLines: false) }
        }
    }

    /// `withLines`: the quantities typed under "Some were short"; without
    /// them every line is received as ordered.
    func receive(_ order: PurchaseOrder, withLines: Bool) async {
        guard !receiving.contains(order.id), !queued.contains(order.id) else { return }
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
        let key = receiveKeys[order.id] ?? "po:\(order.id):" + UUID().uuidString
        receiveKeys[order.id] = key
        let body = ReceiveBody(lines: lines, idempotencyKey: key)
        do {
            let r: ReceiveResponse = try await client.send(
                QueuedWrite.receivePath(order.id), method: .post,
                body: body, retryTransient: false)
            receiveKeys[order.id] = nil
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
        } catch let error as APIClient.APIError where error.isRetryable {
            // Signed for at the back door with no signal: the receive waits
            // in the offline queue with its key (parity audit #23).
            if let write = QueuedWrite.receive(poId: order.id, poNumber: order.poNumber, body) {
                await PendingWriteQueue.shared.enqueue(write)
                receiveKeys[order.id] = nil
                queued.insert(order.id)
                shortOpen = nil
                receivedLine = "\(order.poNumber) kept on this phone. " + RecAnswer.queuedLine + "."
            } else {
                errors[order.id] = error.message
            }
        } catch let error as APIClient.APIError {
            receiveKeys[order.id] = nil
            if PendingWriteQueue.isAlreadyDone(error, path: QueuedWrite.receivePath(order.id)) {
                // Received another way meanwhile — the stock is in.
                receivedLine = "\(order.poNumber) was already received."
                shortOpen = nil
                arrived[order.id] = nil
                await load()
                return
            }
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
    var title = "Recent orders"
    /// Only the orders still to receive (the Receive sheet, and the top of
    /// Send order); counts-only always shows only those.
    var waitingOnly = false
    /// Only the orders already received (the history under Send order's
    /// drafts, when the waiting ones are shown above them).
    var receivedOnly = false

    var body: some View {
        let shown = (waitingOnly || !showMoney) ? viewModel.waiting
            : (receivedOnly ? viewModel.orders.filter(\.isReceived) : viewModel.orders)
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker(title)
            if let line = viewModel.receivedLine, !receivedOnly {
                CavnarMixedText(line, role: .secondary, color: .cavnarGreen)
            }
            if let error = viewModel.loadError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            } else if shown.isEmpty {
                Text(receivedOnly ? "Nothing received yet." : "Nothing waiting to arrive.")
                    .cavnarText(.secondary)
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
        .onReceive(NotificationCenter.default.publisher(for: PendingWriteQueue.didChange)) { _ in
            Task { await viewModel.refreshQueued() }
        }
        // Undo is a window, never a cancel: closing the list commits.
        .onDisappear { viewModel.commitPendingReceives() }
    }

    private func orderRow(_ order: PurchaseOrder) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    (Text(order.supplierName.isEmpty ? order.supplierEmail : order.supplierName).font(.cavnar(.label))
                        + Text(" \u{00B7} ").font(.cavnar(.secondary))
                        + Text(order.poNumber).font(.cavnar(.secondary)))
                        .foregroundStyle(Color.cavnarInk)
                    CavnarMixedText(detail(order), role: .secondary)
                        .cavnarSensitive()
                }
                Spacer(minLength: CavnarSpace.xs)
                if order.isReceived {
                    Text("Received").cavnarText(.label, color: .cavnarGreen)
                } else if viewModel.queued.contains(order.id) {
                    Label("Waiting to send", systemImage: "clock.arrow.circlepath")
                        .cavnarText(.caption, color: .cavnarInk2)
                }
            }
            if !order.isReceived && viewModel.pendingReceive.contains(order.id) {
                // A short Undo before every line goes into stock (F11).
                HStack(spacing: CavnarSpace.s) {
                    Text("Receiving as ordered\u{2026}").cavnarText(.label)
                    Spacer(minLength: CavnarSpace.xs)
                    Button {
                        Haptic.light()
                        viewModel.undoReceive(order)
                    } label: {
                        Text("Undo").frame(minWidth: 88)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .accessibilityHint("Stops \(order.poNumber) being received")
                }
                .frame(minHeight: 44)
            } else if !order.isReceived {
                HStack(spacing: 10) {
                    Button {
                        Haptic.light()
                        viewModel.receiveAsOrdered(order)
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
                            .cavnarText(.label, color: .cavnarEmber2)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
                .disabled(viewModel.receiving.contains(order.id) || viewModel.queued.contains(order.id))
                if viewModel.shortOpen == order.id {
                    shortLines(order)
                }
                if let error = viewModel.errors[order.id] {
                    Text(error).cavnarText(.secondary, color: .cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.vertical, 11)
    }

    private func shortLines(_ order: PurchaseOrder) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Change what arrived, then receive.").cavnarText(.secondary)
            ForEach(order.items) { item in
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.item).cavnarText(.body, color: .cavnarInk)
                        CavnarMixedText("Ordered \(SupplierOrderItem.qtyString(item.qty))\(item.unit.isEmpty ? "" : " \(item.unit)")",
                                        role: .caption)
                    }
                    Spacer(minLength: 8)
                    TextField("0", text: Binding(
                        get: { viewModel.arrivedText(order, item) },
                        set: { viewModel.setArrived($0, order: order, item: item) }))
                        .keyboardType(.decimalPad)
                        .multilineTextAlignment(.trailing)
                        .font(.cavnar(.figureS))
                        .frame(width: 80, height: 44)
                        .padding(.horizontal, 8)
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
