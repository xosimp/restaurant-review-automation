import SwiftUI
import Observation

/// Log waste (U2-32) — what was thrown out, how much and why, POSTed to
/// /mobile/api/food-cost/waste (client_api._do_log_waste). The web has had
/// it on the count sheet (fc2LogWaste); the mobile route existed and the
/// app never called it, so waste seen on the phone only ever reached the
/// ledger as an unexplained recount gap.
@Observable
@MainActor
final class WasteLogViewModel {
    /// client_api.WASTE_REASONS, in the web's order, with its words.
    static let reasons: [(key: String, label: String)] = [
        ("spoiled", "Spoiled"), ("expired", "Expired"), ("overprepped", "Over-prepped"),
        ("dropped", "Dropped"), ("returned", "Sent back"), ("other", "Other"),
    ]

    var ingredientId: Int?
    var qtyText = ""
    var reason = "spoiled"
    var isLogging = false
    var errorMessage: String?
    var loggedLine: String?
    /// Set when the line was kept on the phone for the connection to come
    /// back (parity audit #23) — the walk-in has no signal.
    var queuedLine: String?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    /// Every key /food-cost/waste reads, plus the `idempotency_key` that
    /// makes a resend (the offline queue's replay, a second tap after a lost
    /// answer) land once.
    struct Body: Codable, Equatable {
        let ingredientId: Int
        let qty: Double
        let reason: String
        var idempotencyKey: String? = nil
        enum CodingKeys: String, CodingKey {
            case qty, reason
            case ingredientId = "ingredient_id"
            case idempotencyKey = "idempotency_key"
        }
    }

    /// One key per line of waste, kept until it lands, so a retry of the
    /// same line is the same write — and only the same line: the key is
    /// tied to the body it was made for, so a different line after a
    /// cancelled or failed send never reuses it (the server would answer it
    /// with the first line's result, and it would never be logged —
    /// re-audit 10/8/26 #8).
    private var pendingKey: String?
    private var pendingKeyBody: Body?

    /// The key for this line: the kept one when it was made for exactly
    /// this line, else a new one.
    func key(for line: Body) -> String {
        if let k = pendingKey, pendingKeyBody == line { return k }
        let k = UUID().uuidString
        pendingKey = k
        pendingKeyBody = line
        return k
    }

    private func forgetKey() {
        pendingKey = nil
        pendingKeyBody = nil
    }

    /// Waste lines parked in the offline queue, read back from it — what
    /// the form shows as waiting to send, so a relaunch does not invite the
    /// same line logged twice (#7).
    var parked: [Body] = []

    func refreshParked() async {
        let writes = await PendingWriteQueue.shared.pending(path: QueuedWrite.wastePath)
        parked = writes.compactMap { w in w.bodyJSON.flatMap { try? JSONDecoder().decode(Body.self, from: $0) } }
    }
    private struct Response: Decodable { let ok: Bool; let name: String?; let unit: String?; let error: String? }

    var quantity: Double? {
        guard let q = FoodCostQuickEntryViewModel.parsedPrice(qtyText), q > 0 else { return nil }
        return q
    }

    var canLog: Bool { !isLogging && ingredientId != nil && quantity != nil }

    func log() async -> Bool {
        guard let ingredientId, let qty = quantity else {
            errorMessage = "Pick what and how much."
            return false
        }
        isLogging = true
        errorMessage = nil
        loggedLine = nil
        queuedLine = nil
        defer { isLogging = false }
        let line = Body(ingredientId: ingredientId, qty: qty, reason: reason)
        let body = Body(ingredientId: ingredientId, qty: qty, reason: reason, idempotencyKey: "waste:" + key(for: line))
        do {
            let r: Response = try await client.send("/mobile/api/food-cost/waste", method: .post,
                                                    body: body, retryTransient: false)
            forgetKey()
            guard r.ok else {
                errorMessage = r.error ?? "Couldn\u{2019}t log that."
                return false
            }
            let unit = (r.unit ?? "").isEmpty ? "" : " \(r.unit ?? "")"
            loggedLine = "Logged \(CountSheetViewModel.expectedString(qty))\(unit) of \(r.name ?? "that") as waste."
            qtyText = ""
            await Haptic.success()
            return true
        } catch let error as APIClient.APIError where error.isRetryable {
            // No signal in the walk-in: the line waits in the offline queue
            // with its key, so it lands once whether or not this send did.
            guard let write = QueuedWrite.waste(body) else {
                errorMessage = error.message
                return false
            }
            await PendingWriteQueue.shared.enqueue(write)
            forgetKey()
            queuedLine = "Kept on this phone. " + RecAnswer.queuedLine + "."
            qtyText = ""
            await refreshParked()
            return false
        } catch let error as APIClient.APIError {
            forgetKey()
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t log that."
        }
        return false
    }
}

/// The form itself — on the count sheet, as on the web, and in its own
/// sheet from Food Cost's action row.
struct WasteLogForm: View {
    let items: [CountSheetItem]
    var onLogged: () -> Void = {}
    @State private var viewModel = WasteLogViewModel()
    @FocusState private var qtyFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("LOG WASTE")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            Text("What was thrown out, how much, and why.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
            Picker("What", selection: $viewModel.ingredientId) {
                Text("Pick an ingredient").tag(Int?.none)
                ForEach(items) { it in
                    Text(it.unit.map { $0.isEmpty ? it.name : "\(it.name) (\($0))" } ?? it.name).tag(Int?.some(it.ingredientId))
                }
            }
            .pickerStyle(.menu)
            .tint(Color.cavnarEmber2)
            HStack(spacing: 12) {
                TextField("How much", text: $viewModel.qtyText)
                    .keyboardType(.decimalPad)
                    .font(.cavnarNumber(16, weight: 600))
                    .focused($qtyFocused)
                    .padding(.vertical, 8)
                    .padding(.horizontal, 10)
                    .background(RoundedRectangle(cornerRadius: 8).stroke(Color.cavnarPaper3, lineWidth: 1))
                    .frame(maxWidth: 120)
                Picker("Why", selection: $viewModel.reason) {
                    ForEach(WasteLogViewModel.reasons, id: \.key) { Text($0.label).tag($0.key) }
                }
                .pickerStyle(.menu)
                .tint(Color.cavnarEmber2)
                Spacer(minLength: 0)
            }
            if let error = viewModel.errorMessage {
                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let line = viewModel.loggedLine {
                HomeMixedText.make(line, size: 14, weight: 600, color: .cavnarGreen)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let line = viewModel.queuedLine {
                Label {
                    HomeMixedText.make(line, size: 14, weight: 600, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "clock.arrow.circlepath").foregroundStyle(Color.cavnarInk3)
                }
            }
            if !viewModel.parked.isEmpty {
                parkedList
            }
            Button {
                Haptic.light()
                qtyFocused = false
                Task { if await viewModel.log() { onLogged() } }
            } label: {
                Group {
                    if viewModel.isLogging {
                        CavnarShimmerText(text: "Logging…")
                    } else {
                        Text("Log it")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(!viewModel.canLog)
        }
        .cavnarCard()
        .task { await viewModel.refreshParked() }
        .onReceive(NotificationCenter.default.publisher(for: PendingWriteQueue.didChange)) { _ in
            Task { await viewModel.refreshParked() }
        }
    }

    /// Lines kept on the phone, still waiting for a connection — already
    /// logged as far as the owner is concerned.
    private var parkedList: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Waiting to send")
                .font(.cavnarBody(12.5, weight: 700)).foregroundStyle(Color.cavnarInk3)
            ForEach(Array(viewModel.parked.enumerated()), id: \.offset) { _, line in
                HomeMixedText.make(Self.parkedLine(line, items: items), size: 13.5, weight: 500, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// "2 lb Salmon · Spoiled".
    static func parkedLine(_ line: WasteLogViewModel.Body, items: [CountSheetItem]) -> String {
        let item = items.first { $0.ingredientId == line.ingredientId }
        let unit = (item?.unit ?? "").isEmpty ? "" : " \(item?.unit ?? "")"
        let why = WasteLogViewModel.reasons.first { $0.key == line.reason }?.label ?? line.reason
        return "\(CountSheetViewModel.expectedString(line.qty))\(unit) \(item?.name ?? "an ingredient") \u{00B7} \(why)"
    }
}

/// Log waste on its own, from Food Cost's action row ("inventory/waste").
/// Reads the count sheet for the ingredient list — the same list the web's
/// form offers.
struct WasteLogSheet: View {
    @State private var countSheet = CountSheetViewModel(persistsDraft: false)
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let error = countSheet.errorMessage {
                        Text(error).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if countSheet.isLoading && countSheet.items.isEmpty {
                        CavnarWorkingLine().padding(.vertical, 12)
                    } else if countSheet.items.isEmpty {
                        Text("No ingredients on file yet — waste is logged against an ingredient.")
                            .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                            .cavnarCard()
                    } else {
                        WasteLogForm(items: countSheet.items)
                    }
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.immediately)
            .cavnarModuleBackground()
            .navigationTitle("Log waste")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Log waste")
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
            .task { await countSheet.load() }
        }
    }
}
