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
struct CountSheetDraft: Codable, Equatable {
    var counts: [Int: String]
    var savedAt: Date

    static let maxAge: TimeInterval = 2 * 24 * 60 * 60

    @MainActor
    static var key: String { "count-sheet-draft.u\(SessionScope.userId).r\(SessionScope.restaurantId).json" }

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
}

@Observable
@MainActor
final class CountSheetViewModel {
    var items: [CountSheetItem] = []
    var counts: [Int: String] = [:] {
        didSet { if persistsDraft { persistDraft() } }
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
    private struct SaveResponse: Decodable { let ok: Bool; let written: Int?; let skipped: Int?; let error: String? }
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
        /// only by a count parked offline, so its replay is not filed under
        /// the day the signal came back. Omitted when nil. A parked count
        /// carries no ledger mark: nobody is there to answer the delivery
        /// question when it replays.
        var date: String? = nil
        var ledgerMark: Int? = nil
        /// "counted" (the count includes the delivery) or "after" (it came
        /// after the count, so the server adds it) — only once asked.
        var deliveries: String? = nil
        enum CodingKeys: String, CodingKey {
            case items, deliveries, date
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
            if persistsDraft, !isSynced, let draft = CountSheetDraft.load() {
                var restored = 0
                for (id, text) in draft.counts where fresh[id] != nil && fresh[id] != text {
                    fresh[id] = text
                    restored += 1
                }
                if restored > 0 { restoredAt = draft.savedAt }
            }
            if persistsDraft, isSynced { CountSheetDraft.clear() }
            counts = fresh
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
    var hasUnsaved: Bool { !isSynced && editedCount > 0 }

    /// "12 of 48 changed".
    var progressLine: String { "\(editedCount) of \(items.count) changed" }

    /// ±1 from what the line says now (or the ledger's figure), never below 0.
    func step(_ id: Int, by delta: Double) {
        let base = Double(counts[id] ?? "") ?? items.first(where: { $0.ingredientId == id })?.expected ?? 0
        counts[id] = Self.expectedString(max(0, base + delta))
    }

    /// Throws away the typed counts: the ledger's figures come back.
    func discard() {
        counts = Dictionary(uniqueKeysWithValues: items.map { ($0.ingredientId, Self.expectedString($0.expected)) })
        CountSheetDraft.clear()
        restoredAt = nil
    }

    private func persistDraft() {
        guard !items.isEmpty, !isSynced else { return }
        var edited: [Int: String] = [:]
        for it in items {
            let t = counts[it.ingredientId] ?? ""
            if t != Self.expectedString(it.expected) { edited[it.ingredientId] = t }
        }
        if edited.isEmpty { CountSheetDraft.clear() } else { CountSheetDraft(counts: edited, savedAt: Date()).save() }
    }

    /// `deliveries` answers the server's question ("counted" or "after");
    /// nil on the first save, which is what lets the server ask.
    func save(deliveries: String? = nil) async {
        guard !isSynced else { return }
        let lines = changed
        guard !lines.isEmpty else { errorMessage = "Nothing changed from what the ledger expects."; return }
        isSaving = true; errorMessage = nil; savedCount = nil; queuedCount = nil
        defer { isSaving = false }
        do {
            let body = SaveBody(items: lines, ledgerMark: ledgerMark, deliveries: deliveries)
            let r: SaveResponse = try await client.send("/mobile/api/food-cost/count-sheet", method: .post,
                                                        body: body, retryTransient: false)
            pendingDeliveries = []
            deliveryQuestion = nil
            if r.ok {
                savedCount = r.written ?? lines.count
                CountSheetDraft.clear()
                restoredAt = nil
                await Haptic.success()
                await load()
            } else {
                errorMessage = r.error ?? "Couldn't save the count."
            }
        } catch let error as APIClient.APIError where error.isRetryable && !error.mayHaveReachedServer {
            // Counted in the walk-in with no signal: the count never left
            // the phone, so it waits in the offline queue, dated today on the
            // restaurant's clock when that clock is known (QueuedWrite).
            let day = RestaurantClock.isKnown ? CavnarDate.isoDay(Date(), in: RestaurantClock.timeZone) : nil
            guard let write = QueuedWrite.countSheet(SaveBody(items: lines, date: day)) else {
                errorMessage = error.message
                return
            }
            await PendingWriteQueue.shared.enqueue(write)
            queuedCount = lines.count
            CountSheetDraft.clear()
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
                VStack(alignment: .leading, spacing: 16) {
                    header
                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let n = viewModel.savedCount {
                        Text("\(n) recount\(n == 1 ? "" : "s") saved. Food cost reads them tonight.")
                            .font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarGreen)
                    }
                    if let n = viewModel.queuedCount {
                        Label {
                            HomeMixedText.make("\(n) recount\(n == 1 ? "" : "s") kept on this phone. "
                                               + RecAnswer.queuedLine + ".", size: 15, weight: 600, color: .cavnarInk2)
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
                            .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                            .cavnarCard()
                    } else {
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
                        if !viewModel.isSynced {
                            saveButton
                        }
                        // One line of waste (U2-32), as on the web's count
                        // sheet — synced or not: logged as counted waste
                        // instead of turning up later as an unexplained gap.
                        WasteLogForm(items: viewModel.items) {
                            // Logged waste lowers what the ledger expects;
                            // re-read it, but never over recounts typed and
                            // not yet saved.
                            if viewModel.changed.isEmpty { Task { await viewModel.load() } }
                        }
                    }
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.immediately)
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
                        Text("Done").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .task { await viewModel.load() }
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
        VStack(alignment: .leading, spacing: 8) {
            Text("COUNT SHEET")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            if let source = viewModel.source, source.synced {
                // Synced (parity audit #8): its counts, read-only — count
                // there; this sheet follows.
                HomeMixedText.make(source.line("Counts") + ". Count there; this sheet follows.",
                                   size: 14, weight: 500, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("Filled with what the ledger expects on hand. Change only what differs, then save — each change is a recount.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                if !viewModel.items.isEmpty {
                    HomeMixedText.make(viewModel.lastCountedLine, size: 13, color: .cavnarInk3)
                }
            }
        }
        .cavnarCard()
    }

    private var countModeButton: some View {
        Button {
            Haptic.medium()
            focused = nil
            counting = true
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "rectangle.and.hand.point.up.left").font(.system(size: 14, weight: .semibold))
                Text("Count in the walk-in")
                Spacer(minLength: 6)
                if viewModel.editedCount > 0 {
                    Text(viewModel.progressLine).font(.cavnarNumber(13, weight: 600))
                }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarSecondaryButtonStyle())
        .accessibilityHint("Full screen, with search and large steppers")
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
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(it.name).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk)
                if let u = it.unit, !u.isEmpty {
                    Text(u).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer(minLength: 8)
            if viewModel.isSynced {
                Text(CountSheetViewModel.expectedString(it.expected).isEmpty ? DSRFormat.dash
                     : CountSheetViewModel.expectedString(it.expected))
                    .font(.cavnarNumber(16, weight: 600))
                    .foregroundStyle(Color.cavnarInk2)
                    .accessibilityLabel("\(it.name): \(CountSheetViewModel.expectedString(it.expected))")
            } else {
                TextField("0", text: Binding(
                    get: { viewModel.counts[it.ingredientId] ?? "" },
                    set: { viewModel.counts[it.ingredientId] = $0 }))
                    .keyboardType(.decimalPad)
                    .multilineTextAlignment(.trailing)
                    .font(.cavnarNumber(16, weight: 600))
                    .foregroundStyle((viewModel.counts[it.ingredientId] ?? "") == CountSheetViewModel.expectedString(it.expected)
                                     ? Color.cavnarInk3 : Color.cavnarEmber2)
                    .frame(width: 84)
                    .padding(.vertical, 6)
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
            HomeMixedText.make("Your unsaved count from \(CavnarDate.mdyTime(at)) is back.", size: 13.5, weight: 600,
                               color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 6)
            Button("Start over") {
                Haptic.light()
                viewModel.discard()
            }
            .font(.cavnarBody(13.5, weight: 700))
            .foregroundStyle(Color.cavnarEmber2)
            .buttonStyle(.plain)
        }
        .cavnarCard()
    }

    /// The server's question, with its two answers — the web's buttons.
    private func deliveryPrompt(_ question: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(question)
                .font(.cavnarBody(14.5, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(viewModel.pendingDeliveries) { d in
                HomeMixedText.make("\(d.name): \(CountSheetViewModel.expectedString(d.qty))\(d.unit.map { " \($0)" } ?? "") received",
                                   size: 13.5, weight: 500, color: .cavnarInk3)
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
/// steppers, Next moves to the next line, and "12 of 48 changed" at the
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
                                .font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk3)
                                .cavnarCard()
                        }
                        ForEach(shown) { it in
                            countRow(it).id(it.ingredientId)
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                    .padding(.bottom, 120)
                }
                .onChange(of: focused) { _, id in
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
                        Text("Back").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Your counts stay on the sheet")
                }
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button(nextId == nil ? "Done" : "Next") {
                        Haptic.selection()
                        focused = nextId
                    }
                    .font(.cavnarBody(15, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                }
            }
            .safeAreaInset(edge: .bottom) {
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
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .background(Color.cavnarPaper.opacity(0.94))
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
                Text(viewModel.progressLine).font(.cavnarNumber(18, weight: 700)).foregroundStyle(Color.cavnarInk)
                Spacer()
                Text("Change only what differs").font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                    Capsule().fill(LinearGradient(colors: [Color.cavnarEmber, Color.cavnarEmber2],
                                                  startPoint: .leading, endPoint: .trailing))
                        .frame(width: max(4, geo.size.width * CGFloat(viewModel.editedCount) / CGFloat(total)))
                        .animation(.easeOut(duration: 0.25), value: viewModel.editedCount)
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
                Text(it.name).font(.cavnarBody(17, weight: 600)).foregroundStyle(Color.cavnarInk)
                Spacer(minLength: 8)
                HomeMixedText.make("Expected \(expected.isEmpty ? DSRFormat.dash : expected)\(it.unit.map { $0.isEmpty ? "" : " \($0)" } ?? "")",
                                   size: 12.5, color: .cavnarInk3)
            }
            HStack(spacing: 12) {
                stepButton("minus", label: "One less \(it.name)") { viewModel.step(it.ingredientId, by: -1) }
                TextField(expected.isEmpty ? "0" : expected, text: Binding(
                    get: { viewModel.counts[it.ingredientId] ?? "" },
                    set: { viewModel.counts[it.ingredientId] = $0 }))
                    .keyboardType(.decimalPad)
                    .multilineTextAlignment(.center)
                    .font(.cavnarNumber(26, weight: 700))
                    .foregroundStyle(edited ? Color.cavnarEmber2 : Color.cavnarInk2)
                    .frame(maxWidth: .infinity, minHeight: 56)
                    .background(Color.cavnarPaper3.opacity(0.35), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .focused($focused, equals: it.ingredientId)
                    .accessibilityLabel("Count for \(it.name)")
                stepButton("plus", label: "One more \(it.name)") { viewModel.step(it.ingredientId, by: 1) }
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
