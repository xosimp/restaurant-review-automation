import Foundation
import Observation

/// Drives the "send this order to the supplier who fills it" flow — the
/// step the Food Cost order list used to stop short of.
///
/// Sending emails a supplier, so it is never one tap: the owner can change
/// any quantity, and every send — one supplier or all of them — is
/// confirmed with the supplier, the line count and the total first, as on
/// the web (dashboard.html, the data-so-send confirm). An order that
/// already went comes back `already_sent` and is only sent again on an
/// explicit "Send it again".
@Observable
@MainActor
final class SupplierOrderViewModel {
    var draft: SupplierOrderDraft?
    /// Fingerprint of the draft on screen. It goes back with the send, and
    /// the server refuses (409) a send whose draft has changed since — so
    /// what is emailed is what the owner reviewed (MOD-FC-8).
    var draftHash: String?
    var isLoading = false
    var errorMessage: String?

    /// The owner's quantity for each line, keyed by supplier email then the
    /// line's key (SupplierOrderItem.lineKey). Seeded from the draft.
    var quantities: [String: [String: String]] = [:]

    /// Set while a send is in flight, so the button can't be double-fired.
    var isSending = false
    /// The outcome of the last send, held until the sheet is dismissed.
    var lastResult: SendOrderResult?

    /// The send waiting on the owner's confirmation.
    struct PendingSend: Identifiable {
        /// The suppliers it goes to — one, or every one ("Send all").
        let groups: [SupplierOrderGroup]
        let lineCount: Int
        let total: Double
        /// "Send it again" past the already-sent guard.
        let resend: Bool
        /// The server's own words when this is a resend question.
        let message: String?
        var id: String { groups.map(\.supplierEmail).joined(separator: ",") + (resend ? "#resend" : "") }
    }
    var pendingSend: PendingSend?

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
        let draftHash: String?
        let error: String?

        enum CodingKeys: String, CodingKey {
            case ok, groups, unassigned, error
            case itemCount = "item_count"
            case totalCost = "total_cost"
            case draftHash = "draft_hash"
        }
    }

    // Each view model declares its own — the existing ones (AccountViewModel,
    // GuestTextClubViewModel) are file-private, so this follows the same
    // convention rather than promoting a shared type just for this.
    private typealias OKErrorResponse = APIClient.OKResponse

    func load() async {
        isLoading = draft == nil
        errorMessage = nil
        defer { isLoading = false }
        do {
            let response: DraftResponse = try await client.send("/mobile/api/food-cost/order-draft")
            draft = SupplierOrderDraft(groups: response.groups, unassigned: response.unassigned,
                                       itemCount: response.itemCount, totalCost: response.totalCost)
            draftHash = response.draftHash
            quantities = Dictionary(uniqueKeysWithValues: response.groups.map { g in
                (g.supplierEmail, Dictionary(g.items.map { ($0.lineKey, SupplierOrderItem.qtyString($0.qty)) },
                                             uniquingKeysWith: { a, _ in a }))
            })
        } catch let error as APIClient.APIError {
            if draft == nil { errorMessage = error.message }
        } catch is CancellationError {
            // View went away mid-fetch; not a failure.
        } catch {
            if draft == nil { errorMessage = "Couldn't build the order." }
        }
    }

    // MARK: - Quantities

    func quantityText(_ group: SupplierOrderGroup, _ item: SupplierOrderItem) -> String {
        quantities[group.supplierEmail]?[item.lineKey] ?? SupplierOrderItem.qtyString(item.qty)
    }

    func setQuantity(_ text: String, group: SupplierOrderGroup, item: SupplierOrderItem) {
        quantities[group.supplierEmail, default: [:]][item.lineKey] = text
    }

    /// The typed quantity, or nil when it isn't a number of 0 or more.
    func quantity(_ group: SupplierOrderGroup, _ item: SupplierOrderItem) -> Double? {
        let text = quantityText(group, item).trimmingCharacters(in: .whitespaces)
        guard let v = FoodCostQuickEntryViewModel.parsedPrice(text), v >= 0, v <= 100_000 else { return nil }
        return v
    }

    /// Lines going out (quantity above 0) and their total, at the drafted
    /// unit costs — what the confirmation states.
    func summary(_ group: SupplierOrderGroup) -> (lines: Int, total: Double, valid: Bool) {
        var lines = 0, total = 0.0, valid = true
        for item in group.items {
            guard let q = quantity(group, item) else { valid = false; continue }
            if q > 0 {
                lines += 1
                total += q * (item.unitCost ?? 0)
            }
        }
        return (lines, (total * 100).rounded() / 100, valid)
    }

    /// The lines whose quantity differs from the draft — all the server
    /// needs (inventory.apply_order_edits: a line not named keeps its
    /// drafted quantity).
    private func edits(_ group: SupplierOrderGroup) -> [SendBody.Line] {
        group.items.compactMap { item in
            guard let q = quantity(group, item), abs(q - item.qty) > 0.0005 else { return nil }
            return SendBody.Line(ingredientId: item.ingredientId,
                                 item: item.ingredientId == nil ? item.item : nil, qty: q)
        }
    }

    // MARK: - Send (always confirmed)

    /// Asks before sending one supplier's order.
    func askToSend(_ group: SupplierOrderGroup) {
        let s = summary(group)
        guard s.valid else { errorMessage = "Quantities are numbers, 0 or more."; return }
        guard s.lines > 0 else { errorMessage = "Every line is at 0 — nothing to send."; return }
        errorMessage = nil
        pendingSend = PendingSend(groups: [group], lineCount: s.lines, total: s.total, resend: false, message: nil)
    }

    /// Asks before sending every supplier's order.
    func askToSendAll() {
        guard let groups = draft?.groups, !groups.isEmpty else { return }
        var lines = 0, total = 0.0
        var sendable: [SupplierOrderGroup] = []
        for g in groups {
            let s = summary(g)
            guard s.valid else { errorMessage = "Quantities are numbers, 0 or more."; return }
            if s.lines > 0 {
                sendable.append(g)
                lines += s.lines
                total += s.total
            }
        }
        guard !sendable.isEmpty else { errorMessage = "Every line is at 0 — nothing to send."; return }
        errorMessage = nil
        pendingSend = PendingSend(groups: sendable, lineCount: lines, total: total, resend: false, message: nil)
    }

    /// The confirmation's own sentence: who, how many lines, how much.
    func confirmMessage(_ p: PendingSend) -> String {
        if let message = p.message { return message }
        let who = p.groups.count == 1
            ? "\(p.groups[0].displayName) (\(p.groups[0].supplierEmail))"
            : "\(p.groups.count) suppliers: " + p.groups.map(\.displayName).joined(separator: ", ")
        return "Email \(who) an order for \(p.lineCount) item\(p.lineCount == 1 ? "" : "s"), \(Self.money(p.total))?"
    }

    private struct SendBody: Encodable {
        let supplierEmail: String
        /// The hash of the draft the owner reviewed (MOD-FC-8).
        let draftHash: String?
        let resend: Bool
        let lines: [Line]?
        struct Line: Encodable {
            let ingredientId: Int?
            let item: String?
            let qty: Double
            enum CodingKeys: String, CodingKey { case item, qty; case ingredientId = "ingredient_id" }
        }
        enum CodingKeys: String, CodingKey {
            case resend, lines
            case supplierEmail = "supplier_email"
            case draftHash = "draft_hash"
        }
    }

    /// A refused send's body: `already_sent` asks "Send it again?", `stale`
    /// means the draft moved under the owner.
    private struct RefusalBody: Decodable {
        let error: String?
        let alreadySent: Bool?
        let stale: Bool?
        let failed: [SendOrderResult.FailedOrder]?
        enum CodingKeys: String, CodingKey {
            case error, stale, failed
            case alreadySent = "already_sent"
        }
    }

    /// Runs the confirmed send: one request per supplier, each with that
    /// supplier's own hash and edited lines (the server takes edits for one
    /// supplier at a time). An order that already went is collected and
    /// asked about again rather than sent.
    func confirmSend(_ p: PendingSend) async {
        guard !isSending else { return }
        pendingSend = nil
        isSending = true
        errorMessage = nil
        defer { isSending = false }
        var sent: [SendOrderResult.SentOrder] = []
        var failed: [SendOrderResult.FailedOrder] = []
        var undoMinutes: Int?
        var alreadyWent: [(SupplierOrderGroup, String)] = []
        var stale = false
        for group in p.groups {
            let lines = edits(group)
            let body = SendBody(supplierEmail: group.supplierEmail,
                                draftHash: group.draftHash ?? draftHash,
                                resend: p.resend, lines: lines.isEmpty ? nil : lines)
            do {
                let r: SendOrderResult = try await client.send(
                    "/mobile/api/food-cost/send-order", method: .post, body: body, retryTransient: false)
                sent += r.sent
                failed += r.failed
                if let m = r.undoMinutes { undoMinutes = m }
            } catch let error as APIClient.APIError {
                let refusal = error.decodeBody(RefusalBody.self)
                if refusal?.alreadySent == true, !p.resend {
                    alreadyWent.append((group, refusal?.error ?? error.message))
                } else if refusal?.stale == true {
                    stale = true
                    failed.append(.init(supplierEmail: group.supplierEmail, error: error.message))
                    break
                } else if let listed = refusal?.failed, !listed.isEmpty {
                    failed += listed
                } else {
                    failed.append(.init(supplierEmail: group.supplierEmail, error: error.message))
                }
            } catch is CancellationError {
                break
            } catch {
                failed.append(.init(supplierEmail: group.supplierEmail, error: "Couldn't send the order."))
            }
        }
        lastResult = SendOrderResult(ok: !sent.isEmpty || undoMinutes != nil, sent: sent, failed: failed,
                                     error: nil, undoMinutes: undoMinutes)
        if !sent.isEmpty || undoMinutes != nil { Haptic.success() }
        // The reload re-seeds every quantity from the draft; an order that
        // is asked about again keeps the quantities the owner confirmed.
        let typed = quantities
        await load()
        for (group, _) in alreadyWent {
            if let kept = typed[group.supplierEmail] { quantities[group.supplierEmail] = kept }
        }
        if stale {
            errorMessage = "The order changed since you reviewed it — take another look before sending."
        }
        // Already on an open PO: asked, never re-sent on a guess (DATA-15).
        if !alreadyWent.isEmpty {
            let groups = alreadyWent.map(\.0)
            let message = alreadyWent.count == 1
                ? alreadyWent[0].1
                : "These orders already went: " + groups.map(\.displayName).joined(separator: ", ") + ". Send them again?"
            let fresh = groups.compactMap { g in draft?.groups.first { $0.supplierEmail == g.supplierEmail } }
            let target = fresh.isEmpty ? groups : fresh
            let total = target.reduce(0.0) { $0 + summary($1).total }
            let count = target.reduce(0) { $0 + summary($1).lines }
            pendingSend = PendingSend(groups: target, lineCount: count, total: total, resend: true, message: message)
        }
    }

    static func money(_ v: Double) -> String {
        "$" + v.formatted(.number.precision(.fractionLength(2)))
    }

    // MARK: - Suppliers

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
}
