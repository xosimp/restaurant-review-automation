import Foundation

/// The suggested order list, grouped by the supplier each ingredient is
/// ordered from — see inventory.build_supplier_orders. The quantities are
/// the same ones the Food Cost order list already shows; this only adds
/// "who does it get sent to".
struct SupplierOrderDraft: Decodable {
    let groups: [SupplierOrderGroup]
    /// Items with no supplier assigned yet. They're surfaced rather than
    /// dropped, so the order doesn't silently omit them.
    let unassigned: [SupplierOrderItem]
    let itemCount: Int
    let totalCost: Double

    enum CodingKeys: String, CodingKey {
        case groups, unassigned
        case itemCount = "item_count"
        case totalCost = "total_cost"
    }

    var isEmpty: Bool { groups.isEmpty && unassigned.isEmpty }
}

struct SupplierOrderGroup: Decodable, Identifiable {
    let supplierName: String
    let supplierEmail: String
    let items: [SupplierOrderItem]
    let totalCost: Double

    var id: String { supplierEmail }

    enum CodingKeys: String, CodingKey {
        case supplierName = "supplier_name"
        case supplierEmail = "supplier_email"
        case items
        case totalCost = "total_cost"
    }
}

struct SupplierOrderItem: Decodable, Identifiable {
    let item: String
    let unit: String
    let qty: Int
    let unitCost: Double
    let lineCost: Double
    /// "critical" (out or below par with no cover) or "soon".
    let urgency: String
    let supplierName: String
    let supplierEmail: String

    var id: String { item }
    var isCritical: Bool { urgency == "critical" }

    enum CodingKeys: String, CodingKey {
        case item, unit, qty, urgency
        case unitCost = "unit_cost"
        case lineCost = "line_cost"
        case supplierName = "supplier_name"
        case supplierEmail = "supplier_email"
    }
}

/// A sent order. `items` is what the supplier was actually told to send,
/// which is what receiving confirms against.
struct PurchaseOrder: Decodable, Identifiable {
    let id: Int
    let poNumber: String
    let supplierName: String
    let supplierEmail: String
    let items: [SupplierOrderItem]
    let totalCost: Double
    /// "sent" while outstanding, "received" once closed.
    let status: String
    let sentAt: String
    let receivedAt: String?

    var isReceived: Bool { status == "received" }

    enum CodingKeys: String, CodingKey {
        case id, items, status
        case poNumber = "po_number"
        case supplierName = "supplier_name"
        case supplierEmail = "supplier_email"
        case totalCost = "total_cost"
        case sentAt = "sent_at"
        case receivedAt = "received_at"
    }
}

/// POST /food-cost/send-order — one entry per supplier, since each
/// supplier is its own PO and its own send.
struct SendOrderResult: Decodable {
    let ok: Bool
    let sent: [SentOrder]
    let failed: [FailedOrder]
    let error: String?

    struct SentOrder: Decodable, Identifiable {
        let poNumber: String
        let supplierEmail: String
        let supplierName: String
        let itemCount: Int
        let totalCost: Double

        var id: String { poNumber }

        enum CodingKeys: String, CodingKey {
            case poNumber = "po_number"
            case supplierEmail = "supplier_email"
            case supplierName = "supplier_name"
            case itemCount = "item_count"
            case totalCost = "total_cost"
        }
    }

    struct FailedOrder: Decodable, Identifiable {
        let supplierEmail: String
        let error: String

        var id: String { supplierEmail }

        enum CodingKeys: String, CodingKey {
            case supplierEmail = "supplier_email"
            case error
        }
    }
}
