import SwiftUI
import PhotosUI
import Observation

/// Recipes to confirm — the phone half of the web Food Cost card. Drafts
/// arrive Tuesday mornings for dishes the POS created that have no recipe
/// (recipes.py), and from a photographed recipe card here. Accept writes
/// the recipe; nothing is written until then.
struct RecipeDraftLine: Decodable, Identifiable {
    let ingredientId: Int?
    let name: String
    let qty: Double
    let unit: String?
    let confidence: String?
    var id: String { "\(ingredientId ?? 0)-\(name)" }
    enum CodingKeys: String, CodingKey { case name, qty, unit, confidence; case ingredientId = "ingredient_id" }
}

struct RecipeDraft: Decodable, Identifiable {
    let id: Int
    let menuItemId: Int?
    let menuItemName: String?
    let lines: [RecipeDraftLine]
    let note: String?
    enum CodingKeys: String, CodingKey {
        case id, lines, note
        case menuItemId = "menu_item_id"
        case menuItemName = "menu_item_name"
    }
}

@Observable
@MainActor
final class RecipeDraftsViewModel {
    var drafts: [RecipeDraft] = []
    var isLoading = false
    var isScanning = false
    var busyId: Int?
    var errorMessage: String?
    var lastMessage: String?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    private struct ListResponse: Decodable { let ok: Bool; let drafts: [RecipeDraft] }
    private struct ScanResponse: Decodable { let ok: Bool; let draft: RecipeDraft?; let error: String? }
    private struct OkResponse: Decodable { let ok: Bool; let error: String? }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let r: ListResponse = try await client.send("/mobile/api/food-cost/recipe-drafts")
            drafts = r.drafts
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load recipe drafts."
        }
    }

    func scan(_ item: PhotosPickerItem) async {
        errorMessage = nil; lastMessage = nil
        isScanning = true
        defer { isScanning = false }
        do {
            guard let raw = try await item.loadTransferable(type: Data.self),
                  let jpeg = InvoiceScanViewModel.downscaledJPEG(raw) else {
                errorMessage = "That photo couldn't be read. Try another."
                return
            }
            let r: ScanResponse = try await client.upload("/mobile/api/food-cost/recipes/scan",
                                                          fileData: jpeg, filename: "recipe.jpg", mimeType: "image/jpeg")
            guard r.ok, let d = r.draft else {
                errorMessage = r.error ?? "The card couldn't be read."
                return
            }
            lastMessage = "Draft ready for \(d.menuItemName ?? "the dish") — confirm it below."
            await Haptic.success()
            await load()
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func answer(_ draft: RecipeDraft, accept: Bool) async {
        busyId = draft.id; errorMessage = nil
        defer { busyId = nil }
        do {
            let r: OkResponse = try await client.send(
                "/mobile/api/food-cost/recipe-drafts/\(draft.id)/\(accept ? "accept" : "reject")",
                method: .post, body: [String: String]())
            if r.ok {
                lastMessage = accept ? "Recipe written for \(draft.menuItemName ?? "the dish")." : "Draft removed."
                drafts.removeAll { $0.id == draft.id }
                await Haptic.success()
            } else {
                errorMessage = r.error ?? "Couldn't do that."
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't do that."
        }
    }
}

struct RecipeDraftsSheet: View {
    @State private var viewModel = RecipeDraftsViewModel()
    @State private var pickerItem: PhotosPickerItem?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    intro
                    if viewModel.isScanning {
                        VStack(alignment: .leading, spacing: 10) {
                            CavnarShimmerText(text: "Reading the card…")
                            CavnarSkeletonLines(widths: [1.0, 0.7, 0.55])
                        }
                        .cavnarCard()
                    }
                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let m = viewModel.lastMessage {
                        Text(m).font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarGreen)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if viewModel.isLoading && viewModel.drafts.isEmpty {
                        CavnarWorkingLine().padding(.vertical, 12)
                    } else if viewModel.drafts.isEmpty {
                        Text("Every dish on your POS has a recipe, or drafts arrive Tuesday mornings for any that don't.")
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                            .cavnarCard()
                    } else {
                        ForEach(viewModel.drafts) { draft in
                            draftCard(draft)
                        }
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Recipes")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Recipes")
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
            .onChange(of: pickerItem) { _, item in
                guard let item else { return }
                Task {
                    await viewModel.scan(item)
                    pickerItem = nil
                }
            }
        }
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("RECIPES TO CONFIRM")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            Text("Photograph a recipe card and it becomes a draft here, using only ingredients already on your list. Accept writes the recipe; nothing changes until you do.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            PhotosPicker(selection: $pickerItem, matching: .images) {
                HStack(spacing: 8) {
                    Image(systemName: "camera.viewfinder").font(.system(size: 13, weight: .semibold))
                    Text("Photograph a recipe card")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isScanning))
            .disabled(viewModel.isScanning)
        }
        .cavnarCard()
    }

    private func draftCard(_ draft: RecipeDraft) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(draft.menuItemName ?? "Dish #\(draft.menuItemId ?? 0)")
                .font(.cavnarBody(16, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            if let note = draft.note, !note.isEmpty {
                Text(note).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            VStack(alignment: .leading, spacing: 6) {
                ForEach(draft.lines) { line in
                    HStack(spacing: 8) {
                        Text(line.name).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2)
                        Spacer(minLength: 6)
                        Text(Self.qty(line.qty) + (line.unit.map { " \($0)" } ?? ""))
                            .font(.cavnarNumber(14, weight: 600))
                            .foregroundStyle(line.confidence == "low" ? Color.cavnarAmber : Color.cavnarInk)
                        if line.confidence == "low" {
                            Text("?").font(.cavnarBody(12, weight: 700)).foregroundStyle(Color.cavnarAmber)
                        }
                    }
                }
            }
            HStack(spacing: 10) {
                Button {
                    Haptic.light()
                    Task { await viewModel.answer(draft, accept: false) }
                } label: {
                    Text("Not right").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(viewModel.busyId == draft.id)
                Button {
                    Haptic.light()
                    Task { await viewModel.answer(draft, accept: true) }
                } label: {
                    Text(viewModel.busyId == draft.id ? "Writing…" : "Accept recipe").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.busyId == draft.id))
                .disabled(viewModel.busyId == draft.id)
            }
        }
        .cavnarCard(.ai)
    }

    private static func qty(_ q: Double) -> String {
        q == q.rounded() ? String(Int(q)) : String(format: "%.2f", q).replacingOccurrences(of: #"0+$"#, with: "", options: .regularExpression)
    }
}
