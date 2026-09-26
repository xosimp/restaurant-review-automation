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

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    private struct Body: Encodable {
        let ingredientId: Int
        let qty: Double
        let reason: String
        enum CodingKeys: String, CodingKey { case qty, reason; case ingredientId = "ingredient_id" }
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
        defer { isLogging = false }
        do {
            let r: Response = try await client.send("/mobile/api/food-cost/waste", method: .post,
                                                    body: Body(ingredientId: ingredientId, qty: qty, reason: reason),
                                                    retryTransient: false)
            guard r.ok else {
                errorMessage = r.error ?? "Couldn\u{2019}t log that."
                return false
            }
            let unit = (r.unit ?? "").isEmpty ? "" : " \(r.unit ?? "")"
            loggedLine = "Logged \(CountSheetViewModel.expectedString(qty))\(unit) of \(r.name ?? "that") as waste."
            qtyText = ""
            await Haptic.success()
            return true
        } catch let error as APIClient.APIError {
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
    }
}

/// Log waste on its own, from Food Cost's action row ("inventory/waste").
/// Reads the count sheet for the ingredient list — the same list the web's
/// form offers.
struct WasteLogSheet: View {
    @State private var countSheet = CountSheetViewModel()
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
