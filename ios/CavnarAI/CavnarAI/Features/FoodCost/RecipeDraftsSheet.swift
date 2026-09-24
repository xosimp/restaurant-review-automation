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
    /// H6 (recipes.py): "estimate" (drafted from the ingredient list) or
    /// "transcribed" (read off a card); "plate" or "batch" — a batch line is
    /// divided by the yield on Accept; whether its unit converted to the
    /// ingredient's (false = skipped on an unedited Accept, `unit_note`
    /// says why); and what the card itself said. All absent on older drafts.
    var source: String? = nil
    var per: String? = nil
    var unitOk: Bool? = nil
    var cardQty: Double? = nil
    var cardUnit: String? = nil
    var unitNote: String? = nil
    var id: String { "\(ingredientId ?? 0)-\(name)" }
    enum CodingKeys: String, CodingKey {
        case name, qty, unit, confidence, source, per
        case ingredientId = "ingredient_id"
        case unitOk = "unit_ok"
        case cardQty = "card_qty"
        case cardUnit = "card_unit"
        case unitNote = "unit_note"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ingredientId = try? c.decodeIfPresent(Int.self, forKey: .ingredientId)
        name = try c.decode(String.self, forKey: .name)
        qty = try c.decode(Double.self, forKey: .qty)
        unit = try? c.decodeIfPresent(String.self, forKey: .unit)
        confidence = try? c.decodeIfPresent(String.self, forKey: .confidence)
        source = try? c.decodeIfPresent(String.self, forKey: .source)
        per = try? c.decodeIfPresent(String.self, forKey: .per)
        unitOk = try? c.decodeIfPresent(Bool.self, forKey: .unitOk)
        cardQty = try? c.decodeIfPresent(Double.self, forKey: .cardQty)
        cardUnit = try? c.decodeIfPresent(String.self, forKey: .cardUnit)
        unitNote = try? c.decodeIfPresent(String.self, forKey: .unitNote)
    }

    /// A unit the server could not convert — skipped on an unedited Accept.
    var unitUnconverted: Bool { unitOk == false }
    var isBatch: Bool { per == "batch" }

    /// "card: 2 cups" — what the photographed card said, when it differs
    /// from the converted line.
    var cardLine: String? {
        guard let q = cardQty else { return nil }
        let s = RecipeDraftLine.format(q) + (cardUnit.map { " \($0)" } ?? "")
        let converted = RecipeDraftLine.format(qty) + (unit.map { " \($0)" } ?? "")
        return s == converted ? nil : "card: " + s
    }

    static func format(_ q: Double) -> String {
        q == q.rounded() ? String(Int(q)) : String(format: "%.2f", q).replacingOccurrences(of: #"0+$"#, with: "", options: .regularExpression)
    }

    /// The quiet mark beside a line's quantity, so high and medium no longer
    /// look the same (H6/J9): high — nothing; medium — a small "check" in
    /// ink3; low — the amber "?". Nil for high or an unknown value.
    enum Mark: Equatable { case check, doubt }
    var mark: Mark? {
        switch TrustConfidence.normalisedBand(confidence) {
        case "medium": return .check
        case "low": return .doubt
        default: return nil
        }
    }
}

struct RecipeDraft: Decodable, Identifiable {
    let id: Int
    let menuItemId: Int?
    let menuItemName: String?
    let lines: [RecipeDraftLine]
    let note: String?
    /// H6 (recipes._draft_flags): drafted from the ingredient list rather
    /// than read off a card; a batch card that needs its yield before
    /// Accept; the lines whose units could not be converted; and the
    /// confidence levels its lines carry. Absent on an older server.
    var isEstimate: Bool? = nil
    var needsYield: Bool? = nil
    var unitWarnings: [UnitWarning]? = nil
    var confidenceLevels: [String]? = nil

    struct UnitWarning: Decodable, Hashable {
        let name: String?
        let note: String?
    }

    enum CodingKeys: String, CodingKey {
        case id, lines, note
        case menuItemId = "menu_item_id"
        case menuItemName = "menu_item_name"
        case isEstimate = "is_estimate"
        case needsYield = "needs_yield"
        case unitWarnings = "unit_warnings"
        case confidenceLevels = "confidence_levels"
    }

    /// Accept needs the card's yield first (a batch recipe) — from the
    /// server's flag, else from any line marked per batch.
    var requiresYield: Bool { needsYield ?? lines.contains(where: \.isBatch) }

    /// "Olive oil: tbsp doesn't convert to L" — one per line that will be
    /// skipped on Accept.
    var unitWarningLines: [String] {
        let fromFlags = (unitWarnings ?? []).compactMap { w -> String? in
            guard let name = w.name, !name.isEmpty else { return nil }
            return w.note.map { "\(name): \($0)" } ?? name
        }
        if !fromFlags.isEmpty { return fromFlags }
        return lines.filter(\.unitUnconverted).map { l in l.unitNote.map { "\(l.name): \($0)" } ?? l.name }
    }
}

/// POST …/recipe-drafts/<id>/accept → {ok, written, skipped, edited,
/// unit_skipped, needs_yield, error}. `unit_skipped` names the lines left out
/// because their units could not be converted (H6).
struct RecipeAcceptResult: Decodable {
    let ok: Bool
    var written: Int? = nil
    var skipped: Int? = nil
    var unitSkipped: [String]? = nil
    var needsYield: Bool? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, written, skipped, error
        case unitSkipped = "unit_skipped"
        case needsYield = "needs_yield"
    }

    /// "Recipe written for Margherita — 5 lines. Left out (units didn't
    /// convert): olive oil, basil."
    static func message(dish: String, result: RecipeAcceptResult) -> String {
        var s = "Recipe written for \(dish)"
        if let w = result.written, w > 0 { s += " \u{2014} \(w) line\(w == 1 ? "" : "s")" }
        s += "."
        let skipped = (result.unitSkipped ?? []).filter { !$0.isEmpty }
        if !skipped.isEmpty {
            s += " Left out because their units didn\u{2019}t convert: \(skipped.joined(separator: ", ")) \u{2014} add them by hand."
        }
        return s
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
    private typealias OkResponse = APIClient.OKResponse

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let r: ListResponse = try await client.send("/mobile/api/food-cost/recipe-drafts")
            drafts = r.drafts
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
            // The screen went away mid-load — not a failure (CLIENT-49).
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

    /// What Accept posts: `yield` — how many plates a batch card makes — only
    /// when the draft needs it (H6); the server divides the batch lines by it.
    struct AnswerBody: Encodable {
        let yield: Double?
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encodeIfPresent(yield, forKey: .yield)
        }
        enum CodingKeys: String, CodingKey { case yield }
    }

    func answer(_ draft: RecipeDraft, accept: Bool, yield: Double? = nil) async {
        busyId = draft.id; errorMessage = nil
        defer { busyId = nil }
        do {
            let r: RecipeAcceptResult = try await client.send(
                "/mobile/api/food-cost/recipe-drafts/\(draft.id)/\(accept ? "accept" : "reject")",
                method: .post, body: AnswerBody(yield: accept ? yield : nil))
            if r.ok {
                lastMessage = accept
                    ? RecipeAcceptResult.message(dish: draft.menuItemName ?? "the dish", result: r)
                    : "Draft removed."
                drafts.removeAll { $0.id == draft.id }
                await Haptic.success()
            } else {
                errorMessage = r.error ?? (r.needsYield == true
                    ? "Say how many plates this card makes before accepting." : "Couldn't do that.")
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
    /// The yield typed for each batch draft (H6), by draft id.
    @State private var yieldText: [Int: String] = [:]
    @Environment(\.dismiss) private var dismiss

    /// The typed yield as a positive number, or nil.
    static func parsedYield(_ text: String?) -> Double? {
        guard let t = text?.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: "."),
              let v = Double(t), v > 0, v.isFinite else { return nil }
        return v
    }

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
        let typedYield = Self.parsedYield(yieldText[draft.id])
        let blockedOnYield = draft.requiresYield && typedYield == nil
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(draft.menuItemName ?? "Dish #\(draft.menuItemId ?? 0)")
                    .font(.cavnarBody(16, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                // Drafted from your ingredient list, not read off a card —
                // an estimate until you accept it (H6).
                if draft.isEstimate == true {
                    ClaimKindTag(kind: "estimate")
                }
            }
            if let note = draft.note, !note.isEmpty {
                Text(note).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Lines whose units didn't convert are skipped on Accept (H6) —
            // said before the tap, not after.
            let warnings = draft.unitWarningLines
            if !warnings.isEmpty {
                CavnarCaveat(title: "Units to check",
                             detail: warnings.joined(separator: " \u{00B7} ")
                                + ". These lines are left out when you accept; add them by hand.")
            }
            VStack(alignment: .leading, spacing: 6) {
                ForEach(draft.lines) { line in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 8) {
                            Text(line.name).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2)
                            if line.isBatch {
                                Text("per batch").font(.cavnarBody(11.5, weight: 600)).foregroundStyle(Color.cavnarInk3)
                            }
                            Spacer(minLength: 6)
                            Text(Self.qty(line.qty) + (line.unit.map { " \($0)" } ?? ""))
                                .font(.cavnarNumber(14, weight: 600))
                                .foregroundStyle(line.mark == .doubt || line.unitUnconverted ? Color.cavnarAmber : Color.cavnarInk)
                            if line.unitUnconverted {
                                Text("unit?").font(.cavnarBody(11.5, weight: 700)).foregroundStyle(Color.cavnarAmber)
                                    .accessibilityLabel("The unit didn't convert — skipped on accept")
                            } else {
                                switch line.mark {
                                case .doubt:
                                    Text("?").font(.cavnarBody(12, weight: 700)).foregroundStyle(Color.cavnarAmber)
                                        .accessibilityLabel("Unsure — check this amount")
                                case .check:
                                    Text("check").font(.cavnarBody(11.5, weight: 600)).foregroundStyle(Color.cavnarInk3)
                                        .accessibilityLabel("Worth a check")
                                case nil:
                                    EmptyView()
                                }
                            }
                        }
                        // What the card itself said, when conversion changed it.
                        if let card = line.cardLine {
                            HomeMixedText.make(card, size: 12, color: .cavnarInk3)
                        }
                    }
                }
            }
            // A batch card never said how many plates it makes: its lines
            // are refused as one plate's until the yield is given (H6).
            if draft.requiresYield {
                VStack(alignment: .leading, spacing: 6) {
                    Text("This card is a batch. How many plates does it make?")
                        .font(.cavnarBody(13.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    TextField("Plates", text: Binding(
                        get: { yieldText[draft.id] ?? "" },
                        set: { yieldText[draft.id] = $0 }))
                        .keyboardType(.decimalPad)
                        .cavnarTextFieldStyle()
                        .font(.cavnarNumber(15, weight: 600))
                        .frame(maxWidth: 140, alignment: .leading)
                        .accessibilityLabel("Plates this card makes")
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
                    Task { await viewModel.answer(draft, accept: true, yield: draft.requiresYield ? typedYield : nil) }
                } label: {
                    Text(viewModel.busyId == draft.id ? "Writing…" : "Accept recipe").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.busyId == draft.id || blockedOnYield))
                .disabled(viewModel.busyId == draft.id || blockedOnYield)
            }
        }
        .cavnarCard(.ai)
    }

    private static func qty(_ q: Double) -> String {
        q == q.rounded() ? String(Int(q)) : String(format: "%.2f", q).replacingOccurrences(of: #"0+$"#, with: "", options: .regularExpression)
    }
}
