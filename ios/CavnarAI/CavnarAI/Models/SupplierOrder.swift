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
    /// This supplier's own fingerprint — a send for one supplier carries it,
    /// so another supplier's lines moving does not refuse this one.
    var draftHash: String? = nil

    var id: String { supplierEmail }
    /// The name the owner reads; the address when no name was set.
    var displayName: String { supplierName.isEmpty ? supplierEmail : supplierName }

    enum CodingKeys: String, CodingKey {
        case supplierName = "supplier_name"
        case supplierEmail = "supplier_email"
        case items
        case totalCost = "total_cost"
        case draftHash = "draft_hash"
    }
}

/// One line of a drafted order or a sent PO. Quantities are Doubles: an
/// owner-edited order carries 2.5 cases, which an Int failed to decode —
/// and the purchase-order list with it. The money is optional because a
/// login without FOOD_COST_VIEW receives deliveries with every cost taken
/// off (client_api.purchase_orders_for).
struct SupplierOrderItem: Decodable, Identifiable {
    let item: String
    let unit: String
    let qty: Double
    let unitCost: Double?
    let lineCost: Double?
    /// "critical" (out or below par with no cover) or "soon".
    let urgency: String?
    let supplierName: String?
    let supplierEmail: String?
    let ingredientId: Int?
    /// Last week's waste came off this line's quantity.
    let trimmedForWaste: Bool?

    var id: String { lineKey }
    var isCritical: Bool { urgency == "critical" }
    /// The key the server matches an edited or short line by
    /// (inventory.apply_order_edits, client_api._po_line_key): the
    /// ingredient id, or "name:<item>" for a CSV line with none.
    var lineKey: String { ingredientId.map(String.init) ?? "name:" + item.lowercased() }

    enum CodingKeys: String, CodingKey {
        case item, unit, qty, urgency
        case unitCost = "unit_cost"
        case lineCost = "line_cost"
        case supplierName = "supplier_name"
        case supplierEmail = "supplier_email"
        case ingredientId = "ingredient_id"
        case trimmedForWaste = "trimmed_for_waste"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        item = (try? c.decode(String.self, forKey: .item)) ?? ""
        unit = (try? c.decodeIfPresent(String.self, forKey: .unit)) ?? ""
        qty = (try? c.decode(Double.self, forKey: .qty)) ?? 0
        unitCost = try? c.decodeIfPresent(Double.self, forKey: .unitCost)
        lineCost = try? c.decodeIfPresent(Double.self, forKey: .lineCost)
        urgency = try? c.decodeIfPresent(String.self, forKey: .urgency)
        supplierName = try? c.decodeIfPresent(String.self, forKey: .supplierName)
        supplierEmail = try? c.decodeIfPresent(String.self, forKey: .supplierEmail)
        ingredientId = try? c.decodeIfPresent(Int.self, forKey: .ingredientId)
        trimmedForWaste = try? c.decodeIfPresent(Bool.self, forKey: .trimmedForWaste)
    }

    /// "2", "2.5" — never "2.0".
    static func qtyString(_ v: Double) -> String {
        let r = (v * 1000).rounded() / 1000
        return r == r.rounded() ? String(Int(r)) : String(r)
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
    /// Nil for a login that receives deliveries without the margins.
    let totalCost: Double?
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
    /// Set when the owner's send delay queued the order instead of sending
    /// it — the phone used to skip that undo window entirely (MOD-FC-9).
    let undoMinutes: Int?

    enum CodingKeys: String, CodingKey {
        case ok, sent, failed, error
        case undoMinutes = "undo_minutes"
    }

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
