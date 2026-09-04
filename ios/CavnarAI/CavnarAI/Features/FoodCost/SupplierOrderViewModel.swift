import Foundation
import Observation

/// Drives the "send this order to the supplier who fills it" flow — the
/// step the Food Cost order list used to stop short of.
@Observable
@MainActor
final class SupplierOrderViewModel {
    var draft: SupplierOrderDraft?
    var orders: [PurchaseOrder] = []
    var isLoading = false
    var errorMessage: String?

    /// Set while a send is in flight, so the button can't be double-fired.
    var isSending = false
    /// The outcome of the last send, held until the sheet is dismissed.
    var lastResult: SendOrderResult?

    /// Which ingredient the supplier editor is open for, if any.
    var assigningItem: SupplierOrderItem?
    var isSavingSupplier = false
    var supplierError: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct DraftResponse: Decodable {
        let ok: Bool
        let groups: [SupplierOrderGroup]
        let unassigned: [SupplierOrderItem]
        let itemCount: Int
        let totalCost: Double
        let error: String?

        enum CodingKeys: String, CodingKey {
            case ok, groups, unassigned, error
            case itemCount = "item_count"
            case totalCost = "total_cost"
        }
    }

    private struct OrdersResponse: Decodable { let ok: Bool; let orders: [PurchaseOrder] }

    // Each view model declares its own — the existing ones (AccountViewModel,
    // GuestTextClubViewModel) are file-private, so this follows the same
    // convention rather than promoting a shared type just for this.
    private struct OKErrorResponse: Decodable {
        let ok: Bool
        let error: String?
    }

    func load() async {
        isLoading = draft == nil
        errorMessage = nil
        defer { isLoading = false }
        do {
            let response: DraftResponse = try await client.send("/mobile/api/food-cost/order-draft")
            draft = SupplierOrderDraft(groups: response.groups, unassigned: response.unassigned,
                                       itemCount: response.itemCount, totalCost: response.totalCost)
            // The PO history is secondary — a failure here shouldn't take
            // the order draft down with it.
            let history: OrdersResponse? = try? await client.send(
                "/mobile/api/food-cost/purchase-orders", hapticOnError: false)
            orders = history?.orders ?? []
        } catch let error as APIClient.APIError {
            if draft == nil { errorMessage = error.message }
        } catch is CancellationError {
            // View went away mid-fetch; not a failure.
        } catch {
            if draft == nil { errorMessage = "Couldn't build the order." }
        }
    }

    private struct SendBody: Encodable {
        let supplierEmail: String?
        enum CodingKeys: String, CodingKey { case supplierEmail = "supplier_email" }
    }

    /// `supplierEmail: nil` sends every supplier's order; passing one sends
    /// just that supplier. Reloads afterwards so the draft and the PO list
    /// both reflect what just happened.
    func send(supplierEmail: String? = nil) async {
        guard !isSending else { return }
        isSending = true
        defer { isSending = false }
        do {
            let result: SendOrderResult = try await client.send(
                "/mobile/api/food-cost/send-order", method: .post,
                body: SendBody(supplierEmail: supplierEmail)
            )
            lastResult = result
            if result.ok { Haptic.success() }
            await load()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't send the order."
        }
    }

    private struct SupplierBody: Encodable {
        let name: String
        let supplierName: String
        let supplierEmail: String
        enum CodingKeys: String, CodingKey {
            case name
            case supplierName = "supplier_name"
            case supplierEmail = "supplier_email"
        }
    }

    /// Returns true on success so the caller can dismiss its editor.
    @discardableResult
    func assignSupplier(to ingredient: String, name: String, email: String) async -> Bool {
        isSavingSupplier = true
        supplierError = nil
        defer { isSavingSupplier = false }
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/food-cost/ingredient-supplier", method: .post,
                body: SupplierBody(name: ingredient, supplierName: name, supplierEmail: email)
            )
            if response.ok {
                await load()
                return true
            }
            supplierError = response.error ?? "Couldn't save that supplier."
            return false
        } catch let error as APIClient.APIError {
            supplierError = error.message
            return false
        } catch {
            supplierError = "Couldn't save that supplier."
            return false
        }
    }

    func markReceived(_ order: PurchaseOrder) async {
        do {
            let _: OKErrorResponse = try await client.send(
                "/mobile/api/food-cost/purchase-orders/\(order.id)/received", method: .post)
            await load()
        } catch {
            // Same low-stakes fallback the Connections rows use — the order
            // simply stays open, which is the safe direction to fail in.
        }
    }
}
