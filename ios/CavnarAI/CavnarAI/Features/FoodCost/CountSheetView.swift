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
    var id: Int { ingredientId }
    enum CodingKeys: String, CodingKey { case name, unit, category, expected; case ingredientId = "ingredient_id" }
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

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    private struct ListResponse: Decodable { let ok: Bool; let items: [CountSheetItem] }
    private struct SaveResponse: Decodable { let ok: Bool; let written: Int?; let skipped: Int?; let error: String? }
    struct SaveBody: Encodable {
        struct Line: Encodable {
            let ingredientId: Int; let counted: Double
            enum CodingKeys: String, CodingKey { case counted; case ingredientId = "ingredient_id" }
        }
        let items: [Line]
    }

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

    func save() async {
        let lines = changed
        guard !lines.isEmpty else { errorMessage = "Nothing changed from what the ledger expects."; return }
        isSaving = true; errorMessage = nil; savedCount = nil
        defer { isSaving = false }
        do {
            let r: SaveResponse = try await client.send("/mobile/api/food-cost/count-sheet", method: .post, body: SaveBody(items: lines))
            if r.ok {
                savedCount = r.written ?? lines.count
                await Haptic.success()
                await load()
            } else {
                errorMessage = r.error ?? "Couldn't save the count."
            }
        } catch let error as APIClient.APIError {
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
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSaving || viewModel.changed.isEmpty))
                        .disabled(viewModel.isSaving || viewModel.changed.isEmpty)
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
}
