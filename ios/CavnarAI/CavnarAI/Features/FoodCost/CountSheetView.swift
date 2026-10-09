import SwiftUI
import Observation

/// The count sheet — the phone half of the web Food Cost card. Filled
/// with what the ledger expects on hand; change only what differs, and
/// each change is saved as a recount. The phone is where a count actually
/// happens, so this is the one Food Cost surface that matters more here
/// than on the web.
struct CountSheetItem: Decodable, Identifiable {
    let ingredientId: Int
    let name: String
    let unit: String?
    let category: String?
    let expected: Double?
    /// The newest recount's timestamp — the "last counted" date.
    var lastRecountAt: String? = nil
    /// Who each ingredient is ordered from (the supplier overview reads these).
    var supplierName: String? = nil
    var supplierEmail: String? = nil
    var id: Int { ingredientId }
    enum CodingKeys: String, CodingKey {
        case name, unit, category, expected
        case ingredientId = "ingredient_id"
        case lastRecountAt = "last_recount_at"
        case supplierName = "supplier_name"
        case supplierEmail = "supplier_email"
    }
}

/// Where the counts come from (inventory_sync.status, the count sheet's and
/// the tracker's `source`): once an inventory system has synced, its counts,
/// suppliers and prices are the ledger's, and the phone shows them read-only
/// — the server refuses a recount then too (parity audit #8).
struct InventorySyncSource: Decodable, Equatable {
    let synced: Bool
    let label: String?
    let syncedAt: String?

    enum CodingKeys: String, CodingKey { case synced, label; case syncedAt = "synced_at" }

    init(synced: Bool, label: String? = nil, syncedAt: String? = nil) {
        self.synced = synced; self.label = label; self.syncedAt = syncedAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        synced = (try? c.decode(Bool.self, forKey: .synced)) ?? false
        label = (try? c.decodeIfPresent(String.self, forKey: .label)) ?? nil
        syncedAt = (try? c.decodeIfPresent(String.self, forKey: .syncedAt)) ?? nil
    }

    var name: String { (label?.isEmpty == false ? label! : "your inventory system") }

    /// "synced 10/6/26" — M/D/YY, nil without a time.
    var syncedOn: String? {
        guard let at = syncedAt, at.count >= 10 else { return nil }
        return "synced " + CavnarDate.mdy(String(at.prefix(10)))
    }

    /// "Counts from MarketMan · synced 10/6/26" (the web's words).
    func line(_ what: String) -> String {
        "\(what) from \(name)" + (syncedOn.map { " \u{00B7} \($0)" } ?? "")
    }
}

/// A delivery received after the count sheet was opened — the server asks
/// whether the count includes it rather than guessing (strategy_routes
/// _do_count_sheet_save, re-audit F2-7).
struct CountSheetDelivery: Decodable, Identifiable {
    let ingredientId: Int
    let qty: Double
    let name: String
    let unit: String?
    var id: Int { ingredientId }
    enum CodingKeys: String, CodingKey { case qty, name, unit; case ingredientId = "ingredient_id" }
}

/// The counts typed and not yet saved, kept on the phone so a walk-in count
/// survives a dismissed sheet, a crash or a phone call (parity audit #23).
/// Scoped to the signed-in user and location; a draft older than
/// `maxAge` is a different count and is dropped.
///
/// A count parked offline keeps its draft until the server has it
/// (`queuedWriteId`, cleared by PendingWriteQueue's acknowledgement): the
/// queue may give the write up — refused, a day old, another location — and
/// the counts come back on the sheet instead of vanishing (re-audit 10/8/26
/// #2). A location switch spares every draft (SessionStore, by `keyPrefix`);
/// sign-out purges them with the rest of the cache (#3).
struct CountSheetDraft: Codable, Equatable {
    var counts: [Int: String]
    var savedAt: Date
    /// Where the ledger stood when these counts were started — sent with
    /// the save, so a delivery posted since is asked about even when the
    /// sheet is reopened later (a reload brings a newer mark).
    var ledgerMark: Int? = nil
    /// When the counts were taken, once they were parked: the server
    /// applies what was posted after it on top of the count (#1).
    var countedAt: Date? = nil
    /// The parked write carrying these counts, while it waits.
    var queuedWriteId: UUID? = nil

    static let maxAge: TimeInterval = 2 * 24 * 60 * 60
    /// Every draft's key starts with this — what a location switch spares.
    static let keyPrefix = "count-sheet-draft."

    @MainActor
    static var key: String { "\(keyPrefix)u\(SessionScope.userId).r\(SessionScope.restaurantId).json" }

    @MainActor
    static func load(now: Date = Date()) -> CountSheetDraft? {
        guard let data = SecureCache.read(key: key),
              let d = try? JSONDecoder().decode(CountSheetDraft.self, from: data) else { return nil }
        guard now.timeIntervalSince(d.savedAt) < maxAge, !d.counts.isEmpty else {
            clear()
            return nil
        }
        return d
    }

    @MainActor
    func save() {
        if let data = try? JSONEncoder().encode(self) { SecureCache.write(data, key: Self.key) }
    }

    @MainActor
    static func clear() { SecureCache.delete(key: key) }

    /// The parked write `id` landed: its counts are on the server, so the
    /// draft it carried goes — only that draft, never one typed since.
    @MainActor
    static func acknowledgeQueued(_ id: UUID) {
        guard let data = SecureCache.read(key: key),
              let d = try? JSONDecoder().decode(CountSheetDraft.self, from: data),
              d.queuedWriteId == id else { return }
        clear()
    }
}

@Observable
@MainActor
final class CountSheetViewModel {
    var items: [CountSheetItem] = []
    var counts: [Int: String] = [:] {
        didSet { if persistsDraft && !applying { countsEdited() } }
    }
    var isLoading = false
    var isSaving = false
    var errorMessage: String?
    var savedCount: Int?
    /// Where the ledger stood when this sheet opened. Sent with the save so
    /// a delivery received in between is asked about (the web does the
    /// same); the phone used to send items alone, so the question never
    /// came and a count taken before the truck silently lost the delivery.
    var ledgerMark: Int?
    /// Set when the server asked: did a delivery arrive after this sheet
    /// was opened, and does the count include it?
    var pendingDeliveries: [CountSheetDelivery] = []
    var deliveryQuestion: String?
    /// The inventory system's sync, when it has one (parity audit #8).
    var source: InventorySyncSource?
    /// When a saved draft was put back on load — said on the sheet.
    var restoredAt: Date?
    /// The parked write holding this sheet's counts while it waits to send
    /// (the draft keeps the counts until it lands, #2).
    var queuedWriteId: UUID?
    /// Parked counts the queue gave up on, back on the sheet to save again.
    var returnedFromQueue = false
    /// After a save, the edited lines that were not saved — kept on the
    /// sheet and named here (#9).
    var notSavedLine: String?
    /// Lines a newer count already covered (the server's `superseded`).
    var supersededCount: Int?
    /// From a restored draft: the mark its counts were started against, and
    /// when they were taken (#1). Nil for counts typed now.
    private var draftMark: Int?
    private var draftCountedAt: Date?
    /// True while the sheet itself sets `counts` (a load, a discard) —
    /// only the owner's own edits move the draft.
    private var applying = false
    /// The waste form and the supplier overview read the sheet without
    /// writing a draft of their own.
    private let persistsDraft: Bool

    private let client: APIClient
    init(client: APIClient = .shared, persistsDraft: Bool = true) {
        self.client = client
        self.persistsDraft = persistsDraft
    }

    private struct ListResponse: Decodable {
        let ok: Bool
        let items: [CountSheetItem]
        let ledgerMark: Int?
        let source: InventorySyncSource?
        enum CodingKeys: String, CodingKey { case ok, items, source; case ledgerMark = "ledger_mark" }
    }
    private struct SaveResponse: Decodable {
        let ok: Bool; let written: Int?; let skipped: Int?; let error: String?
        var skippedIds: [Int]? = nil
        var superseded: Int? = nil
        enum CodingKeys: String, CodingKey { case ok, written, skipped, error, superseded; case skippedIds = "skipped_ids" }
    }
    private struct ConfirmResponse: Decodable {
        let needsConfirm: Bool?
        let deliveries: [CountSheetDelivery]?
        let error: String?
        let code: String?
        let source: InventorySyncSource?
        enum CodingKeys: String, CodingKey { case deliveries, error, code, source; case needsConfirm = "needs_confirm" }
    }
    struct SaveBody: Encodable {
        struct Line: Encodable {
            let ingredientId: Int; let counted: Double
            enum CodingKeys: String, CodingKey { case counted; case ingredientId = "ingredient_id" }
        }
        let items: [Line]
        /// The day the count was taken (ISO, the restaurant's clock) — sent
        /// by a count parked offline (or put back after the queue gave it
        /// up), so its replay is not filed under the day the signal came
        /// back. Omitted when nil.
        var date: String? = nil
        /// When the count was taken (ISO 8601, UTC) — sent with `date`. The
        /// server applies what was posted after it (a delivery, a waste
        /// line) on top of the count instead of letting the late row erase
        /// it (re-audit 10/8/26 #1).
        var countedAt: String? = nil
        /// Where the ledger stood when the sheet (or the kept draft) was
        /// opened. A parked count carries it too: a delivery posted between
        /// that and the count is asked about when the counts come back.
        var ledgerMark: Int? = nil
        /// "counted" (the count includes the delivery) or "after" (it came
        /// after the count, so the server adds it) — only once asked.
        var deliveries: String? = nil
        enum CodingKeys: String, CodingKey {
            case items, deliveries, date
            case countedAt = "counted_at"
            case ledgerMark = "ledger_mark"
        }
    }

    /// Synced from an inventory system: counted there, read here.
    var isSynced: Bool { source?.synced == true }

    /// The newest recount on the sheet, M/D/YY — "Not counted yet" when none.
    var lastCountedLine: String {
        let last = items.compactMap { $0.lastRecountAt.map { String($0.prefix(10)) } }.max()
        guard let last, !last.isEmpty else { return "Not counted yet" }
        return "Last counted \(CavnarDate.mdy(last))"
    }
    /// Recounts parked in the offline queue by the last save.
    var queuedCount: Int?

    /// `counted_at`'s form: ISO 8601 in UTC, which the server reads.
    nonisolated static func isoStamp(_ d: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: d)
    }

    nonisolated static func expectedString(_ v: Double?) -> String {
        guard let v else { return "" }
        let r = (v * 100).rounded() / 100
        return r == r.rounded() ? String(Int(r)) : String(r)
    }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let r: ListResponse = try await client.send("/mobile/api/food-cost/count-sheet")
            items = r.items
            ledgerMark = r.ledgerMark
            source = r.source
            var fresh = Dictionary(uniqueKeysWithValues: r.items.map { ($0.ingredientId, Self.expectedString($0.expected)) })
            // Put back what was typed and not saved — only for ingredients
            // still on the sheet, and never over a synced sheet.
            restoredAt = nil
            returnedFromQueue = false
            queuedWriteId = nil
            queuedCount = nil
            draftMark = nil
            draftCountedAt = nil
            if persistsDraft, !isSynced, var draft = CountSheetDraft.load() {
                var restored = 0
                for (id, text) in draft.counts where fresh[id] != nil && fresh[id] != text {
                    fresh[id] = text
                    restored += 1
                }
                draftMark = draft.ledgerMark
                draftCountedAt = draft.countedAt
                if let parked = draft.queuedWriteId {
                    if await PendingWriteQueue.shared.isPending(parked) {
                        // Still waiting to send: shown as kept, not unsaved.
                        queuedWriteId = parked
                        queuedCount = draft.counts.count
                    } else {
                        // The queue gave it up (refused, a day old, another
                        // location) — back on the sheet to save again, with
                        // the server's question if it asked one (#1, #2).
                        returnedFromQueue = true
                        draft.queuedWriteId = nil
                        draft.save()
                    }
                }
                if restored == 0 && queuedWriteId == nil {
                    // Nothing in it differs from the ledger any more.
                    CountSheetDraft.clear()
                    draftMark = nil
                    draftCountedAt = nil
                    returnedFromQueue = false
                } else if restored > 0 && queuedWriteId == nil && !returnedFromQueue {
                    restoredAt = draft.savedAt
                }
            }
            if persistsDraft, isSynced { CountSheetDraft.clear() }
            applying = true
            counts = fresh
            applying = false
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
            // The screen went away mid-load — not a failure (CLIENT-49).
        } catch {
            errorMessage = "Couldn't load the count sheet."
        }
    }

    /// Only lines the owner changed are recounts; an untouched line is
    /// the ledger's own figure and saying it again would be a false count.
    var changed: [SaveBody.Line] {
        items.compactMap { it in
            let text = counts[it.ingredientId] ?? ""
            guard text != Self.expectedString(it.expected), let v = Double(text), v >= 0 else { return nil }
            return .init(ingredientId: it.ingredientId, counted: v)
        }
    }

    /// Lines whose figure differs from the ledger's — typed, valid or not —
    /// what the dismiss guard protects.
    var editedCount: Int {
        items.filter { (counts[$0.ingredientId] ?? "") != Self.expectedString($0.expected) }.count
    }
    /// Counts parked in the queue are kept, not unsaved: the sheet closes
    /// without asking, and they send when the signal is back.
    var hasUnsaved: Bool { !isSynced && editedCount > 0 && queuedWriteId == nil }

    /// Edited lines that are not a usable count (text that is not a number
    /// of 0 or more) — never sent, so never "saved".
    var unusable: [CountSheetItem] {
        let usable = Set(changed.map(\.ingredientId))
        return items.filter {
            (counts[$0.ingredientId] ?? "") != Self.expectedString($0.expected) && !usable.contains($0.ingredientId)
        }
    }

    /// "12 of 48 changed".
    var progressLine: String { "\(editedCount) of \(items.count) changed" }

    /// Lines the owner has been through in the walk-in — stepped, typed, or
    /// moved past with the count as it stood (re-audit F15). A line that
    /// matches the shelf is counted too; progress used to move only when a
    /// figure changed, so a walk-in that agreed with the ledger read 0 done.
    var checked: Set<Int> = []

    func markChecked(_ id: Int) { checked.insert(id) }

    /// Lines checked or changed, out of the sheet.
    var checkedCount: Int {
        items.filter { checked.contains($0.ingredientId)
            || (counts[$0.ingredientId] ?? "") != Self.expectedString($0.expected) }.count
    }

    /// "12 of 48 checked · 3 changed".
    var walkInProgressLine: String {
        "\(checkedCount) of \(items.count) checked \u{00B7} \(editedCount) changed"
    }

    /// ±1 from what the line says now (or the ledger's figure), never below 0.
    func step(_ id: Int, by delta: Double) {
        let base = Double(counts[id] ?? "") ?? items.first(where: { $0.ingredientId == id })?.expected ?? 0
        counts[id] = Self.expectedString(max(0, base + delta))
    }

    /// Throws away the typed counts: the ledger's figures come back — and a
    /// count still parked in the queue is taken back with them (#2).
    func discard() {
        if let parked = queuedWriteId {
            Task { await PendingWriteQueue.shared.cancel(parked) }
        }
        queuedWriteId = nil
        queuedCount = nil
        returnedFromQueue = false
        draftMark = nil
        draftCountedAt = nil
        notSavedLine = nil
        applying = true
        counts = Dictionary(uniqueKeysWithValues: items.map { ($0.ingredientId, Self.expectedString($0.expected)) })
        applying = false
        checked = []
        CountSheetDraft.clear()
        restoredAt = nil
    }

    /// The owner changed a figure. Editing counts that were parked takes the
    /// parked write back — the sheet holds them now, and the next save sends
    /// them all — and an edit is a count taken now, not when the draft was
    /// (#1). The draft keeps its first ledger mark, so a delivery posted
    /// since is still asked about.
    private func countsEdited() {
        if let parked = queuedWriteId {
            queuedWriteId = nil
            queuedCount = nil
            Task { await PendingWriteQueue.shared.cancel(parked) }
        }
        draftCountedAt = nil
        if draftMark == nil { draftMark = ledgerMark }
        persistDraft()
    }

    private func persistDraft() {
        guard !items.isEmpty, !isSynced else { return }
        var edited: [Int: String] = [:]
        for it in items {
            let t = counts[it.ingredientId] ?? ""
            if t != Self.expectedString(it.expected) { edited[it.ingredientId] = t }
        }
        if edited.isEmpty {
            CountSheetDraft.clear()
        } else {
            CountSheetDraft(counts: edited, savedAt: Date(), ledgerMark: draftMark ?? ledgerMark,
                            countedAt: draftCountedAt, queuedWriteId: queuedWriteId).save()
        }
    }

    /// The offline queue changed. A parked count that is no longer waiting
    /// either landed (its draft was cleared on acknowledgement) or was given
    /// up (its draft is still here): the sheet says which.
    func queueChanged() async {
        guard let parked = queuedWriteId, !(await PendingWriteQueue.shared.isPending(parked)) else { return }
        let n = queuedCount
        queuedWriteId = nil
        queuedCount = nil
        if CountSheetDraft.load() == nil {
            savedCount = n
            await load()
        } else {
            returnedFromQueue = true
            persistDraft()
        }
    }

    /// The edited lines a save left behind, and the sentence naming them.
    static func notSavedSentence(_ names: [String]) -> String? {
        guard !names.isEmpty else { return nil }
        let n = names.count
        let shown = names.prefix(3).joined(separator: ", ") + (n > 3 ? " and \(n - 3) more" : "")
        return "\(n) line\(n == 1 ? "" : "s") not saved \u{2014} \(shown). Each needs a number of 0 or more; "
            + "\(n == 1 ? "it stays" : "they stay") on the sheet."
    }

    /// `deliveries` answers the server's question ("counted" or "after");
    /// nil on the first save, which is what lets the server ask.
    func save(deliveries: String? = nil) async {
        guard !isSynced else { return }
        let lines = changed
        guard !lines.isEmpty else { errorMessage = "Nothing changed from what the ledger expects."; return }
        isSaving = true; errorMessage = nil; savedCount = nil; notSavedLine = nil; supersededCount = nil
        defer { isSaving = false }
        // A kept count saved later is still the count it was: dated and
        // timed when it was taken, against the mark it was started on.
        let mark = draftMark ?? ledgerMark
        let takenDay = draftCountedAt.flatMap { at in
            RestaurantClock.isKnown ? CavnarDate.isoDay(at, in: RestaurantClock.timeZone) : nil
        }
        do {
            let body = SaveBody(items: lines, date: takenDay, countedAt: draftCountedAt.map(Self.isoStamp),
                                ledgerMark: mark, deliveries: deliveries)
            let r: SaveResponse = try await client.send("/mobile/api/food-cost/count-sheet", method: .post,
                                                        body: body, retryTransient: false)
            pendingDeliveries = []
            deliveryQuestion = nil
            if r.ok {
                // What the save left behind stays on the sheet, named (#9):
                // lines that were never a number, and lines the server skipped.
                let skipped = Set(r.skippedIds ?? [])
                var keep: [Int: String] = [:]
                for it in unusable { keep[it.ingredientId] = counts[it.ingredientId] }
                for id in skipped { if let t = counts[id] { keep[id] = t } }
                let keptNames = items.filter { keep[$0.ingredientId] != nil }.map(\.name)
                // Saved live: a copy still parked would land on top later.
                if let parked = queuedWriteId { await PendingWriteQueue.shared.cancel(parked) }
                queuedWriteId = nil
                queuedCount = nil
                returnedFromQueue = false
                draftMark = nil
                draftCountedAt = nil
                savedCount = r.written ?? lines.count
                supersededCount = (r.superseded ?? 0) > 0 ? r.superseded : nil
                CountSheetDraft.clear()
                restoredAt = nil
                await Haptic.success()
                await load()
                for (id, text) in keep where items.contains(where: { $0.ingredientId == id }) {
                    counts[id] = text
                }
                notSavedLine = Self.notSavedSentence(keptNames)
            } else {
                errorMessage = r.error ?? "Couldn't save the count."
            }
        } catch let error as APIClient.APIError where error.isRetryable && !error.mayHaveReachedServer {
            // Counted in the walk-in with no signal: the count never left
            // the phone, so it waits in the offline queue, dated and timed
            // when it was taken (QueuedWrite) — and its draft stays until the
            // server has it (#2).
            let takenAt = draftCountedAt ?? Date()
            let day = RestaurantClock.isKnown ? CavnarDate.isoDay(takenAt, in: RestaurantClock.timeZone) : nil
            guard let write = QueuedWrite.countSheet(SaveBody(items: lines, date: day, countedAt: Self.isoStamp(takenAt),
                                                              ledgerMark: mark, deliveries: deliveries)) else {
                errorMessage = error.message
                return
            }
            if let older = queuedWriteId { await PendingWriteQueue.shared.cancel(older) }
            queuedWriteId = await PendingWriteQueue.shared.enqueue(write)
            queuedCount = lines.count
            draftMark = mark
            draftCountedAt = takenAt
            persistDraft()
        } catch let error as APIClient.APIError {
            if error.status == 409, let ask = error.decodeBody(ConfirmResponse.self) {
                // Synced since the sheet opened: say so and go read-only.
                if ask.code == "inventory_synced" {
                    source = ask.source ?? InventorySyncSource(synced: true)
                    CountSheetDraft.clear()
                    errorMessage = ask.error
                    return
                }
                if deliveries == nil, ask.needsConfirm == true, let arrived = ask.deliveries, !arrived.isEmpty {
                    pendingDeliveries = arrived
                    deliveryQuestion = ask.error ?? "A delivery was received after you opened this sheet — does your count include it?"
                    return
                }
            }
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't save the count."
        }
    }
}

struct CountSheetView: View {
    @State private var viewModel = CountSheetViewModel()
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focused: Int?
    @State private var confirmingLeave = false
    @State private var counting = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.m) {
                    header
                    if let error = viewModel.errorMessage {
                        Text(error).cavnarText(.body, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let n = viewModel.savedCount {
                        CavnarMixedText("\(n) recount\(n == 1 ? "" : "s") saved. Food cost reads them tonight."
                                        + (viewModel.supersededCount.map { " \($0) already counted again since, so the newer count stands." } ?? ""),
                                        role: .body, color: .cavnarGreen)
                    }
                    if let line = viewModel.notSavedLine {
                        CavnarMixedText(line, role: .body, color: .cavnarAmber)
                    }
                    if viewModel.returnedFromQueue {
                        Label {
                            Text("Your kept count couldn\u{2019}t be sent, so it\u{2019}s back on the sheet. Save it again.")
                                .cavnarText(.body)
                                .fixedSize(horizontal: false, vertical: true)
                        } icon: {
                            Image(systemName: "arrow.uturn.backward.circle").foregroundStyle(Color.cavnarEmber2)
                        }
                        .cavnarCard()
                    }
                    if let n = viewModel.queuedCount {
                        Label {
                            CavnarMixedText("\(n) recount\(n == 1 ? "" : "s") kept on this phone. "
                                            + RecAnswer.queuedLine + ".", role: .body)
                        } icon: {
                            Image(systemName: "clock.arrow.circlepath").foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    if let at = viewModel.restoredAt {
                        restoredNote(at)
                    }
                    if let question = viewModel.deliveryQuestion {
                        deliveryPrompt(question)
                    }
                    if viewModel.isLoading && viewModel.items.isEmpty {
                        CavnarWorkingLine().padding(.vertical, 12)
                    } else if viewModel.items.isEmpty {
                        Text("No ingredients on file yet — add the fifteen or twenty you buy most weeks first.")
                            .cavnarText(.body)
                            .fixedSize(horizontal: false, vertical: true)
                            .cavnarCard()
                    } else {
                        // The walk-in count is the way to count (iOS
                        // readability round, 10/8/26): search, big steppers,
                        // Next. The list below is for a quick fix. Waste is
                        // logged from Food Cost's own Waste button, not from
                        // inside the count.
                        if !viewModel.isSynced {
                            countModeButton
                        }
                        VStack(spacing: 0) {
                            ForEach(Array(viewModel.items.enumerated()), id: \.element.id) { i, it in
                                row(it)
                                if i < viewModel.items.count - 1 { AccountRowDivider() }
                            }
                        }
                        .cavnarCard()
                    }
                }
                .padding(CavnarSpace.gutter)
            }
            .scrollDismissesKeyboard(.immediately)
            // Save in thumb reach, not after the whole list.
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if !viewModel.isSynced && !viewModel.items.isEmpty {
                    CavnarPinnedBar { saveButton }
                }
            }
            .cavnarModuleBackground()
            .navigationTitle("Count sheet")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Count sheet")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        if viewModel.hasUnsaved { confirmingLeave = true } else { dismiss() }
                    } label: {
                        Text("Done").cavnarText(.label, color: .cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .task { await viewModel.load() }
            // A parked count landing, or given up, while the sheet is open.
            .onReceive(NotificationCenter.default.publisher(for: PendingWriteQueue.didChange)) { _ in
                Task { await viewModel.queueChanged() }
            }
            // A swipe down never throws a count away (parity audit #23):
            // with counts typed, the sheet only closes through Done.
            .interactiveDismissDisabled(viewModel.hasUnsaved)
            .confirmationDialog("You have \(viewModel.editedCount) count\(viewModel.editedCount == 1 ? "" : "s") not saved",
                                isPresented: $confirmingLeave, titleVisibility: .visible) {
                Button("Keep them on this phone for later") { dismiss() }
                Button("Discard \(viewModel.editedCount) count\(viewModel.editedCount == 1 ? "" : "s")", role: .destructive) {
                    viewModel.discard()
                    dismiss()
                }
                Button("Keep counting", role: .cancel) {}
            } message: {
                Text("Kept counts come back the next time you open the sheet.")
            }
            .fullScreenCover(isPresented: $counting) {
                WalkInCountView(viewModel: viewModel) { save in
                    counting = false
                    if save { Task { await viewModel.save() } }
                }
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if let source = viewModel.source, source.synced {
                // Synced (parity audit #8): its counts, read-only — count
                // there; this sheet follows.
                CavnarMixedText(source.line("Counts") + ". Count there; this sheet follows.", role: .body)
            } else {
                Text("Change only what\u{2019}s different, then save.")
                    .cavnarText(.lead)
                    .fixedSize(horizontal: false, vertical: true)
                Text("Each line starts at what the ledger expects on hand; each change is a recount.")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if !viewModel.items.isEmpty {
                    CavnarMixedText(viewModel.lastCountedLine, role: .caption)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The walk-in count: the primary until a recount is typed — then the
    /// pinned Save is, and this steps down to "back to the walk-in".
    private var countModeButton: some View {
        Button {
            Haptic.medium()
            focused = nil
            counting = true
        } label: {
            HStack(spacing: CavnarSpace.xs) {
                Image(systemName: "rectangle.and.hand.point.up.left").accessibilityHidden(true)
                Text(viewModel.editedCount > 0 ? "Back to the walk-in count" : "Count in the walk-in")
                Spacer(minLength: CavnarSpace.xxs)
                if viewModel.editedCount > 0 {
                    Text(viewModel.progressLine).font(.cavnar(.figureS))
                }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(WalkInButtonStyle(primary: viewModel.editedCount == 0))
        .accessibilityHint("Full screen, with search and large steppers")
    }

    private struct WalkInButtonStyle: ButtonStyle {
        let primary: Bool
        @ViewBuilder
        func makeBody(configuration: Configuration) -> some View {
            if primary {
                CavnarPrimaryButtonStyle().makeBody(configuration: configuration)
            } else {
                CavnarSecondaryButtonStyle().makeBody(configuration: configuration)
            }
        }
    }

    private var saveButton: some View {
        Button {
            Haptic.light()
            focused = nil
            Task { await viewModel.save() }
        } label: {
            Text(viewModel.isSaving ? "Saving…" : (viewModel.changed.isEmpty ? "Nothing changed" : "Save \(viewModel.changed.count) recount\(viewModel.changed.count == 1 ? "" : "s")"))
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSaving || viewModel.changed.isEmpty
                                              || viewModel.deliveryQuestion != nil))
        .disabled(viewModel.isSaving || viewModel.changed.isEmpty || viewModel.deliveryQuestion != nil)
    }

    @ViewBuilder
    private func row(_ it: CountSheetItem) -> some View {
        HStack(spacing: CavnarSpace.s) {
            VStack(alignment: .leading, spacing: 2) {
                Text(it.name).cavnarText(.body, color: .cavnarInk)
                if let u = it.unit, !u.isEmpty {
                    Text(u).cavnarText(.caption)
                }
            }
            Spacer(minLength: CavnarSpace.xs)
            if viewModel.isSynced {
                Text(CountSheetViewModel.expectedString(it.expected).isEmpty ? DSRFormat.dash
                     : CountSheetViewModel.expectedString(it.expected))
                    .cavnarText(.figureS, color: .cavnarInk2)
                    .accessibilityLabel("\(it.name): \(CountSheetViewModel.expectedString(it.expected))")
            } else {
                TextField("0", text: Binding(
                    get: { viewModel.counts[it.ingredientId] ?? "" },
                    set: { viewModel.counts[it.ingredientId] = $0 }))
                    .keyboardType(.decimalPad)
                    .multilineTextAlignment(.trailing)
                    .font(.cavnar(.figureS))
                    .foregroundStyle((viewModel.counts[it.ingredientId] ?? "") == CountSheetViewModel.expectedString(it.expected)
                                     ? Color.cavnarInk2 : Color.cavnarEmber2)
                    .frame(width: 84, height: 44)
                    .padding(.horizontal, 10)
                    .background(Color.cavnarPaper3.opacity(0.35))
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    .focused($focused, equals: it.ingredientId)
            }
        }
        .padding(.vertical, 9)
    }

    private func restoredNote(_ at: Date) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: "tray.and.arrow.up").foregroundStyle(Color.cavnarInk3).accessibilityHidden(true)
            CavnarMixedText("Your unsaved count from \(CavnarDate.mdyTime(at)) is back.", role: .secondary)
            Spacer(minLength: CavnarSpace.xxs)
            Button {
                Haptic.light()
                viewModel.discard()
            } label: {
                Text("Start over").cavnarText(.label, color: .cavnarEmber2).cavnarHitTarget()
            }
            .buttonStyle(.plain)
        }
        .cavnarCard()
    }

    /// The server's question, with its two answers — the web's buttons.
    private func deliveryPrompt(_ question: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(question)
                .cavnarText(.label)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(viewModel.pendingDeliveries) { d in
                CavnarMixedText("\(d.name): \(CountSheetViewModel.expectedString(d.qty))\(d.unit.map { " \($0)" } ?? "") received",
                                role: .secondary)
            }
            HStack(spacing: 10) {
                Button {
                    Haptic.light()
                    Task { await viewModel.save(deliveries: "counted") }
                } label: {
                    Text("Yes, it\u{2019}s in the count").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                Button {
                    Haptic.light()
                    Task { await viewModel.save(deliveries: "after") }
                } label: {
                    Text("No, it came after").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            }
            .disabled(viewModel.isSaving)
        }
        .cavnarCard()
    }
}

// MARK: - Walk-in count mode (parity audit #41)

/// The count, full screen, for the walk-in: search the sheet, big −/+
/// steppers, Next moves to the next line, and "12 of 48 checked · 3 changed" at the
/// top. Every change lands in the same view model (and its draft on the
/// phone), so leaving this screen never loses a figure.
struct WalkInCountView: View {
    @Bindable var viewModel: CountSheetViewModel
    var close: (_ save: Bool) -> Void
    @State private var search = ""
    @FocusState private var focused: Int?

    private var shown: [CountSheetItem] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return viewModel.items }
        return viewModel.items.filter {
            $0.name.lowercased().contains(q) || ($0.category ?? "").lowercased().contains(q)
        }
    }

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        progress
                        if shown.isEmpty {
                            Text("Nothing on the sheet matches \u{201C}\(search)\u{201D}.")
                                .cavnarText(.body)
                                .cavnarCard()
                        }
                        ForEach(shown) { it in
                            countRow(it).id(it.ingredientId)
                        }
                    }
                    .padding(.horizontal, CavnarSpace.m)
                    .padding(.vertical, CavnarSpace.s)
                }
                .onChange(of: focused) { old, id in
                    // Moving off a line — Next, Done or another line —
                    // confirms it as counted, changed or not (F15).
                    if let old { viewModel.markChecked(old) }
                    guard let id else { return }
                    withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo(id, anchor: .center) }
                }
            }
            .scrollDismissesKeyboard(.interactively)
            .searchable(text: $search, placement: .navigationBarDrawer(displayMode: .always), prompt: "Find an ingredient")
            .cavnarModuleBackground()
            .navigationTitle("Walk-in count")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Walk-in count")
                cavnarToolbarItem(placement: .topBarLeading) {
                    Button {
                        Haptic.light()
                        focused = nil
                        close(false)
                    } label: {
                        Text("Back").cavnarText(.label, color: .cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Your counts stay on the sheet")
                }
                cavnarKeyboardTrailing {
                    Button(nextId == nil ? "Done" : "Next") {
                        Haptic.selection()
                        focused = nextId
                    }
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarEmber2)
                }
            }
            .cavnarPinnedBar {
                Button {
                    Haptic.medium()
                    focused = nil
                    close(true)
                } label: {
                    Text(viewModel.changed.isEmpty ? "Nothing changed yet"
                         : "Save \(viewModel.changed.count) recount\(viewModel.changed.count == 1 ? "" : "s")")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.changed.isEmpty))
                .disabled(viewModel.changed.isEmpty)
            }
        }
    }

    /// The line after the focused one, in the order shown.
    private var nextId: Int? {
        guard let f = focused, let i = shown.firstIndex(where: { $0.ingredientId == f }), i + 1 < shown.count else { return nil }
        return shown[i + 1].ingredientId
    }

    private var progress: some View {
        let total = max(viewModel.items.count, 1)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                CavnarMixedText(viewModel.walkInProgressLine, role: .label)
                Spacer()
                Text("Change only what\u{2019}s different").cavnarText(.caption)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                    Capsule().fill(LinearGradient(colors: [Color.cavnarEmber, Color.cavnarEmber2],
                                                  startPoint: .leading, endPoint: .trailing))
                        .frame(width: max(4, geo.size.width * CGFloat(viewModel.checkedCount) / CGFloat(total)))
                        .animation(.easeOut(duration: 0.25), value: viewModel.checkedCount)
                }
            }
            .frame(height: 6)
            .accessibilityHidden(true)
        }
        .cavnarCard()
    }

    private func countRow(_ it: CountSheetItem) -> some View {
        let text = viewModel.counts[it.ingredientId] ?? ""
        let expected = CountSheetViewModel.expectedString(it.expected)
        let edited = text != expected
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(it.name).cavnarText(.lead)
                Spacer(minLength: CavnarSpace.xs)
                CavnarMixedText("Expected \(expected.isEmpty ? DSRFormat.dash : expected)\(it.unit.map { $0.isEmpty ? "" : " \($0)" } ?? "")",
                                role: .caption)
            }
            HStack(spacing: 12) {
                stepButton("minus", label: "One less \(it.name)") {
                    viewModel.step(it.ingredientId, by: -1)
                    viewModel.markChecked(it.ingredientId)
                }
                TextField(expected.isEmpty ? "0" : expected, text: Binding(
                    get: { viewModel.counts[it.ingredientId] ?? "" },
                    set: { viewModel.counts[it.ingredientId] = $0 }))
                    .keyboardType(.decimalPad)
                    .multilineTextAlignment(.center)
                    .font(.cavnar(.figureM))
                    .foregroundStyle(edited ? Color.cavnarEmber2 : Color.cavnarInk2)
                    .frame(maxWidth: .infinity, minHeight: 56)
                    .background(Color.cavnarPaper3.opacity(0.35), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .focused($focused, equals: it.ingredientId)
                    .accessibilityLabel("Count for \(it.name)")
                stepButton("plus", label: "One more \(it.name)") {
                    viewModel.step(it.ingredientId, by: 1)
                    viewModel.markChecked(it.ingredientId)
                }
            }
        }
        .padding(14)
        .background(Color.cavnarPaper2.opacity(0.6), in: RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
            .strokeBorder(edited ? Color.cavnarEmber.opacity(0.5) : Color.cavnarPaper3.opacity(0.5), lineWidth: 1))
    }

    private func stepButton(_ symbol: String, label: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.selection()
            action()
        } label: {
            Image(systemName: symbol)
                .font(.system(size: 22, weight: .bold))
                .foregroundStyle(Color.cavnarEmber2)
                .frame(width: 60, height: 56)
                .background(Color.cavnarEmber.opacity(0.14), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}
