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

/// What this phone logs as waste most, and last — the "Your usual" rows
/// the form leads with, so a line of waste is three taps, not a scroll
/// through the whole ingredient list. Kept on the phone (UserDefaults):
/// a convenience, never a record.
enum WasteLogHistory {
    private static let key = "foodcost.wasteLog.history"

    private struct Entry: Codable { var count: Int; var last: Date }

    private static func read() -> [Int: Entry] {
        guard let data = UserDefaults.standard.data(forKey: key),
              let map = try? JSONDecoder().decode([Int: Entry].self, from: data) else { return [:] }
        return map
    }

    static func record(_ ingredientId: Int) {
        var map = read()
        var e = map[ingredientId] ?? Entry(count: 0, last: Date())
        e.count += 1
        e.last = Date()
        map[ingredientId] = e
        if let data = try? JSONEncoder().encode(map) { UserDefaults.standard.set(data, forKey: key) }
    }

    /// Ingredient ids, most logged first, then most recent.
    static func usual(limit: Int = 5) -> [Int] {
        read().sorted { a, b in
            a.value.count != b.value.count ? a.value.count > b.value.count : a.value.last > b.value.last
        }
        .prefix(limit).map(\.key)
    }
}

/// Log waste in ten seconds (iOS readability round, 10/8/26): pick what —
/// "Your usual" first, or search — how much with a stepper, why with one
/// tap, then Log it. It used to be a menu picker over the whole ingredient
/// list with no search, and a secondary "Log it". Opened from Food Cost's
/// Waste button; it no longer rides inside the count sheet.
struct WasteLogForm: View {
    let items: [CountSheetItem]
    var onLogged: () -> Void = {}
    @State private var viewModel = WasteLogViewModel()
    @State private var search = ""
    @State private var usual: [Int] = []
    @FocusState private var qtyFocused: Bool
    @FocusState private var searchFocused: Bool

    private static let listShown = 8

    private var picked: CountSheetItem? {
        viewModel.ingredientId.flatMap { id in items.first { $0.ingredientId == id } }
    }

    private var usualItems: [CountSheetItem] {
        usual.compactMap { id in items.first { $0.ingredientId == id } }
    }

    private var matches: [CountSheetItem] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return items }
        return items.filter { $0.name.lowercased().contains(q) || ($0.category ?? "").lowercased().contains(q) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.l) {
            whatStep
            if picked != nil {
                howMuchStep
                whyStep
            }
            if let error = viewModel.errorMessage {
                Text(error).cavnarText(.body, color: .cavnarRedText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let line = viewModel.loggedLine {
                CavnarMixedText(line, role: .body, color: .cavnarGreen)
            }
            if let line = viewModel.queuedLine {
                Label {
                    CavnarMixedText(line, role: .body)
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
                let id = viewModel.ingredientId
                Task {
                    let logged = await viewModel.log()
                    // Logged, or kept on the phone to send: either way the
                    // owner logged it — it joins "Your usual".
                    if let id, logged || viewModel.queuedLine != nil {
                        WasteLogHistory.record(id)
                        usual = WasteLogHistory.usual()
                    }
                    if logged { onLogged() }
                }
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
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !viewModel.canLog))
            .disabled(!viewModel.canLog)
        }
        .task {
            usual = WasteLogHistory.usual()
            await viewModel.refreshParked()
        }
        .onReceive(NotificationCenter.default.publisher(for: PendingWriteQueue.didChange)) { _ in
            Task { await viewModel.refreshParked() }
        }
    }

    // MARK: What

    @ViewBuilder
    private var whatStep: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("What was thrown out")
            if let picked {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    Text(picked.name).cavnarText(.lead)
                    if let u = picked.unit, !u.isEmpty { Text(u).cavnarText(.secondary) }
                    Spacer(minLength: CavnarSpace.xs)
                    Button {
                        Haptic.light()
                        viewModel.ingredientId = nil
                        searchFocused = true
                    } label: {
                        Text("Change").cavnarText(.label, color: .cavnarEmber2).cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
                .cavnarCard()
            } else {
                HStack(spacing: CavnarSpace.xs) {
                    Image(systemName: "magnifyingglass").foregroundStyle(Color.cavnarInk3).accessibilityHidden(true)
                    TextField("Find an ingredient", text: $search)
                        .font(.cavnar(.body))
                        .foregroundStyle(Color.cavnarInk)
                        .focused($searchFocused)
                        .submitLabel(.search)
                }
                .padding(.horizontal, CavnarSpace.s)
                .frame(minHeight: 44)
                .background(Color.cavnarPaper3.opacity(0.35), in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                if search.trimmingCharacters(in: .whitespaces).isEmpty, !usualItems.isEmpty {
                    Text("Your usual").cavnarText(.label, color: .cavnarInk2)
                        .padding(.top, CavnarSpace.xxs)
                    pickList(usualItems)
                    Text("Everything").cavnarText(.label, color: .cavnarInk2)
                        .padding(.top, CavnarSpace.xxs)
                }
                let rows = matches
                if rows.isEmpty {
                    Text("Nothing matches \u{201C}\(search)\u{201D}.").cavnarText(.secondary)
                } else {
                    pickList(Array(rows.prefix(Self.listShown)))
                    if rows.count > Self.listShown {
                        CavnarMoreDisclosure(hiddenCount: rows.count - Self.listShown) {
                            pickList(Array(rows.dropFirst(Self.listShown)))
                        }
                    }
                }
            }
        }
    }

    private func pickList(_ rows: [CountSheetItem]) -> some View {
        VStack(spacing: 0) {
            ForEach(Array(rows.enumerated()), id: \.element.id) { index, it in
                if index > 0 { AccountRowDivider() }
                Button {
                    Haptic.selection()
                    viewModel.ingredientId = it.ingredientId
                    searchFocused = false
                    qtyFocused = true
                } label: {
                    HStack(spacing: CavnarSpace.xs) {
                        Text(it.name).cavnarText(.body, color: .cavnarInk)
                        Spacer(minLength: CavnarSpace.xs)
                        if let u = it.unit, !u.isEmpty { Text(u).cavnarText(.caption) }
                    }
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, CavnarSpace.s)
        .background(Color.cavnarPaper2.opacity(0.6), in: RoundedRectangle(cornerRadius: CavnarRadius.card))
    }

    // MARK: How much

    private var howMuchStep: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("How much")
            HStack(spacing: CavnarSpace.s) {
                stepButton("minus", label: "Less") { step(-1) }
                TextField("0", text: $viewModel.qtyText)
                    .keyboardType(.decimalPad)
                    .multilineTextAlignment(.center)
                    .font(.cavnar(.figureM))
                    .foregroundStyle(Color.cavnarInk)
                    .focused($qtyFocused)
                    .frame(maxWidth: .infinity, minHeight: 56)
                    .background(Color.cavnarPaper3.opacity(0.35), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .accessibilityLabel("How much")
                stepButton("plus", label: "More") { step(1) }
            }
            if let u = picked?.unit, !u.isEmpty {
                Text("In \(u)").cavnarText(.caption)
            }
        }
    }

    private func step(_ by: Double) {
        let now = FoodCostQuickEntryViewModel.parsedPrice(viewModel.qtyText) ?? 0
        let next = max(0, now + by)
        viewModel.qtyText = next == 0 ? "" : CountSheetViewModel.expectedString(next)
    }

    private func stepButton(_ symbol: String, label: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.selection()
            action()
        } label: {
            Image(systemName: symbol)
                .font(.cavnar(.figureS))
                .foregroundStyle(Color.cavnarEmber2)
                .frame(width: 56, height: 56)
                .background(Color.cavnarEmber.opacity(0.14), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    // MARK: Why

    private var whyStep: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Why")
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: CavnarSpace.xs) {
                    ForEach(WasteLogViewModel.reasons, id: \.key) { r in
                        let on = viewModel.reason == r.key
                        Button {
                            Haptic.selection()
                            viewModel.reason = r.key
                        } label: {
                            Text(r.label)
                                .cavnarText(.label, color: on ? .cavnarInk : .cavnarInk2)
                                .padding(.horizontal, CavnarSpace.s)
                                .frame(minHeight: 44)
                                .background(on ? Color.cavnarEmber.opacity(0.28) : Color.cavnarPaper3.opacity(0.35),
                                            in: Capsule())
                                .overlay(Capsule().strokeBorder(on ? Color.cavnarEmber2 : Color.clear, lineWidth: 1))
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(on ? .isSelected : [])
                    }
                }
            }
        }
    }

    /// Lines kept on the phone, still waiting for a connection — already
    /// logged as far as the owner is concerned.
    private var parkedList: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            Text("Waiting to send").cavnarText(.label, color: .cavnarInk2)
            ForEach(Array(viewModel.parked.enumerated()), id: \.offset) { _, line in
                CavnarMixedText(Self.parkedLine(line, items: items), role: .secondary)
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

/// Log waste on its own, from Food Cost's Waste button ("inventory/waste").
/// Reads the count sheet for the ingredient list — the same list the web's
/// form offers.
struct WasteLogSheet: View {
    @State private var countSheet = CountSheetViewModel(persistsDraft: false)
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.m) {
                    if let error = countSheet.errorMessage {
                        Text(error).cavnarText(.body, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if countSheet.isLoading && countSheet.items.isEmpty {
                        CavnarWorkingLine().padding(.vertical, 12)
                    } else if countSheet.items.isEmpty {
                        Text("No ingredients on file yet — waste is logged against an ingredient.")
                            .cavnarText(.body)
                            .fixedSize(horizontal: false, vertical: true)
                            .cavnarCard()
                    } else {
                        WasteLogForm(items: countSheet.items)
                    }
                }
                .padding(CavnarSpace.gutter)
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
                        Text("Done").cavnarText(.label, color: .cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .task { await countSheet.load() }
        }
    }
}
