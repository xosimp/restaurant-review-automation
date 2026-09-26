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
    var id: Int { ingredientId }
    enum CodingKeys: String, CodingKey {
        case name, unit, category, expected
        case ingredientId = "ingredient_id"
        case lastRecountAt = "last_recount_at"
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

@Observable
@MainActor
final class CountSheetViewModel {
    var items: [CountSheetItem] = []
    var counts: [Int: String] = [:]
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

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    private struct ListResponse: Decodable {
        let ok: Bool
        let items: [CountSheetItem]
        let ledgerMark: Int?
        enum CodingKeys: String, CodingKey { case ok, items; case ledgerMark = "ledger_mark" }
    }
    private struct SaveResponse: Decodable { let ok: Bool; let written: Int?; let skipped: Int?; let error: String? }
    private struct ConfirmResponse: Decodable {
        let needsConfirm: Bool?
        let deliveries: [CountSheetDelivery]?
        let error: String?
        enum CodingKeys: String, CodingKey { case deliveries, error; case needsConfirm = "needs_confirm" }
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

    /// The newest recount on the sheet, M/D/YY — "Not counted yet" when none.
    var lastCountedLine: String {
        let last = items.compactMap { $0.lastRecountAt.map { String($0.prefix(10)) } }.max()
        guard let last, !last.isEmpty else { return "Not counted yet" }
        return "Last counted \(CavnarDate.mdy(last))"
    }
    /// Recounts parked in the offline queue by the last save.
    var queuedCount: Int?

    static func expectedString(_ v: Double?) -> String {
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
            counts = Dictionary(uniqueKeysWithValues: r.items.map { ($0.ingredientId, Self.expectedString($0.expected)) })
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

    /// `deliveries` answers the server's question ("counted" or "after");
    /// nil on the first save, which is what lets the server ask.
    func save(deliveries: String? = nil) async {
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
        } catch let error as APIClient.APIError {
            if error.status == 409, deliveries == nil,
               let ask = error.decodeBody(ConfirmResponse.self), ask.needsConfirm == true,
               let arrived = ask.deliveries, !arrived.isEmpty {
                pendingDeliveries = arrived
                deliveryQuestion = ask.error ?? "A delivery was received after you opened this sheet — does your count include it?"
                return
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

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("COUNT SHEET")
                            .font(.cavnarBody(13.5, weight: 700))
                            .tracking(1.2)
                            .foregroundStyle(Color.cavnarEmber2)
                        Text("Filled with what the ledger expects on hand. Change only what differs, then save — each change is a recount.")
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .cavnarCard()
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
                        VStack(spacing: 0) {
                            ForEach(Array(viewModel.items.enumerated()), id: \.element.id) { i, it in
                                HStack(spacing: 12) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(it.name).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk)
                                        if let u = it.unit, !u.isEmpty {
                                            Text(u).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                                        }
                                    }
                                    Spacer(minLength: 8)
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
                                        .background(Color.white.opacity(0.04))
                                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                                        .focused($focused, equals: it.ingredientId)
                                }
                                .padding(.vertical, 9)
                                if i < viewModel.items.count - 1 { AccountRowDivider() }
                            }
                        }
                        .cavnarCard()
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

                        // One line of waste (U2-32), as on the web's count
                        // sheet: logged as counted waste instead of turning
                        // up later as an unexplained recount gap.
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
                        dismiss()
                    } label: {
                        Text("Done").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .task { await viewModel.load() }
        }
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
