import SwiftUI
import PhotosUI
import UIKit
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

/// One dish and its lines as the inventory system loaded them
/// (GET /food-cost/recipes — strategy_routes._do_recipes_list).
struct SyncedRecipe: Decodable, Identifiable, Hashable {
    struct Line: Decodable, Hashable {
        let name: String?
        let qty: Double?
        let unit: String?
    }
    let id: Int?
    let name: String
    let sellPrice: Double?
    let lines: [Line]
    enum CodingKeys: String, CodingKey { case id, name, lines; case sellPrice = "sell_price" }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? c.decodeIfPresent(Int.self, forKey: .id)) ?? nil
        name = (try? c.decode(String.self, forKey: .name)) ?? "A dish"
        sellPrice = (try? c.decodeIfPresent(Double.self, forKey: .sellPrice)) ?? nil
        lines = (try? c.decodeIfPresent([Line].self, forKey: .lines)) ?? []
    }
}

struct SyncedRecipesResponse: Decodable {
    let ok: Bool
    let recipes: [SyncedRecipe]
    let source: InventorySyncSource?
}

@Observable
@MainActor
final class RecipeDraftsViewModel {
    var drafts: [RecipeDraft] = []
    /// Once an inventory system has synced, every dish's recipe is its own
    /// — listed read-only here, and nothing is drafted (parity audit #78).
    var recipes: [SyncedRecipe] = []
    var source: InventorySyncSource?
    var isSynced: Bool { source?.synced == true }
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
        if let r: SyncedRecipesResponse = try? await client.send("/mobile/api/food-cost/recipes", hapticOnError: false),
           r.ok {
            source = r.source
            recipes = r.source?.synced == true ? r.recipes : []
        }
        guard !isSynced else { drafts = []; return }
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

    /// A recipe card from the document camera (parity audit #92): its first
    /// page, flattened and squared by VisionKit, read like a photo.
    func scan(pages: [UIImage]) async {
        guard let first = pages.first, let raw = first.jpegData(compressionQuality: 0.9) else { return }
        if pages.count > 1 {
            lastMessage = "Reading the first page \u{2014} scan one card at a time."
        }
        await read(raw)
    }

    func scan(_ item: PhotosPickerItem) async {
        errorMessage = nil; lastMessage = nil
        guard let raw = try? await item.loadTransferable(type: Data.self) else {
            errorMessage = "That photo couldn't be read. Try another."
            return
        }
        await read(raw)
    }

    private func read(_ raw: Data) async {
        errorMessage = nil
        isScanning = true
        defer { isScanning = false }
        do {
            guard let jpeg = InvoiceScanViewModel.downscaledJPEG(raw) else {
                errorMessage = "That photo couldn't be read. Try another."
                return
            }
            // A job this screen polls (`async`, AI cost audit 10/7/26 #57);
            // an older server's direct answer passes straight through.
            let started: APIClient.AIJobAnswer<ScanResponse> = try await client.upload(
                "/mobile/api/food-cost/recipes/scan", fileData: jpeg, filename: "recipe.jpg",
                mimeType: "image/jpeg", query: ["async": "1"])
            let r: ScanResponse = try await client.resolveAIJob(started)
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

    // MARK: - Not right, with a short Undo (re-audit F12)

    /// How long "Not right" waits before the draft is deleted, so Undo can
    /// stop it.
    static let undoSeconds: Double = 4
    /// A draft the owner said is not right, hidden and waiting out its Undo.
    var pendingReject: RecipeDraft?
    private var rejectTask: Task<Void, Never>?

    /// The drafts on screen — the one waiting out its Undo is hidden.
    var visibleDrafts: [RecipeDraft] { drafts.filter { $0.id != pendingReject?.id } }

    func rejectWithUndo(_ draft: RecipeDraft) {
        // One at a time: an earlier "Not right" goes now.
        commitPendingReject()
        errorMessage = nil
        lastMessage = nil
        pendingReject = draft
        rejectTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(RecipeDraftsViewModel.undoSeconds))
            guard !Task.isCancelled, let self else { return }
            self.commitPendingReject()
        }
    }

    func undoReject() {
        rejectTask?.cancel()
        rejectTask = nil
        pendingReject = nil
    }

    /// Sends the waiting "Not right" now — its window ran out, or the sheet
    /// is closing (Undo is a window, never a cancel).
    func commitPendingReject() {
        rejectTask?.cancel()
        rejectTask = nil
        guard let draft = pendingReject else { return }
        pendingReject = nil
        Task { await self.answer(draft, accept: false) }
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
    @State private var showingCamera = false
    /// The yield typed for each batch draft (H6), by draft id.
    @State private var yieldText: [Int: String] = [:]
    /// Drafts whose every line is open.
    @State private var linesExpanded: Set<Int> = []
    @Environment(\.dismiss) private var dismiss

    /// Lines shown on a draft before "+N more".
    private static let linesShown = 4

    /// The typed yield as a positive number, or nil.
    static func parsedYield(_ text: String?) -> Double? {
        guard let t = text?.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: "."),
              let v = Double(t), v > 0, v.isFinite else { return nil }
        return v
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.m) {
                    if viewModel.isSynced {
                        syncedRecipes
                    } else {
                    intro
                    if viewModel.isScanning {
                        VStack(alignment: .leading, spacing: 10) {
                            CavnarShimmerText(text: "Reading the card…")
                            CavnarSkeletonLines(widths: [1.0, 0.7, 0.55])
                        }
                        .cavnarCard()
                    }
                    if let error = viewModel.errorMessage {
                        Text(error).cavnarText(.body, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let m = viewModel.lastMessage {
                        CavnarMixedText(m, role: .body, color: .cavnarGreen)
                    }
                    if let removed = viewModel.pendingReject {
                        undoRow(removed)
                    }
                    if viewModel.isLoading && viewModel.drafts.isEmpty {
                        CavnarWorkingLine().padding(.vertical, CavnarSpace.s)
                    } else if viewModel.visibleDrafts.isEmpty && viewModel.pendingReject == nil {
                        Text("Every dish on your POS has a recipe, or drafts arrive Tuesday mornings for any that don't.")
                            .cavnarText(.body)
                            .fixedSize(horizontal: false, vertical: true)
                            .cavnarCard()
                    } else {
                        ForEach(viewModel.visibleDrafts) { draft in
                            draftCard(draft, pinned: draft.id == viewModel.visibleDrafts.first?.id)
                        }
                        CavnarWebLinkRow(title: "Recipe drafts",
                                         subtitle: "Every draft line by line, under This week\u{2019}s work",
                                         path: "inventory", actionLabel: "Open on the web")
                    }
                    }
                }
                .padding(CavnarSpace.gutter)
            }
            // The first draft's answer in thumb reach (iOS readability
            // round, 10/8/26); any others keep theirs on their cards.
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if !viewModel.isSynced, let first = viewModel.visibleDrafts.first {
                    CavnarPinnedBar(note: viewModel.visibleDrafts.count > 1 ? (first.menuItemName ?? "The first draft") : nil) {
                        answerButtons(first)
                    }
                }
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
                        Text("Done").cavnarText(.label, color: .cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .task { await viewModel.load() }
            // A "Not right" still in its Undo window goes when the sheet does.
            .onDisappear { viewModel.commitPendingReject() }
            .onChange(of: pickerItem) { _, item in
                guard let item else { return }
                Task {
                    await viewModel.scan(item)
                    pickerItem = nil
                }
            }
            .fullScreenCover(isPresented: $showingCamera) {
                DocumentCameraView { pages in
                    showingCamera = false
                    guard !pages.isEmpty else { return }
                    Task { await viewModel.scan(pages: pages) }
                }
                .ignoresSafeArea()
            }
        }
    }

    /// The inventory system's recipes, read-only (parity audit #78). The
    /// phone says where they come from and how many; the list itself is a
    /// table for the web (iOS readability round, 10/8/26).
    private var syncedRecipes: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            let n = viewModel.recipes.count
            Text(n == 0 ? "No recipes came over with the last sync."
                 : "\(n) recipe\(n == 1 ? "" : "s") from your inventory system")
                .cavnarText(.lead)
                .fixedSize(horizontal: false, vertical: true)
            if let source = viewModel.source {
                CavnarMixedText("From \(source.name)" + (source.syncedOn.map { " \u{00B7} \($0)" } ?? "")
                                + ". Every plate cost and depletion reads these; change them there.", role: .secondary)
            }
            if n > 0 {
                CavnarWebLinkRow(title: "Your recipes", subtitle: "Every dish and its ingredients",
                                 path: "inventory/menu", actionLabel: "Open on the web")
            }
        }
        .cavnarCard()
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            Text("Scan a recipe card and it becomes a draft here, using only ingredients already on your list. Accept writes the recipe; nothing changes until you do.")
                .cavnarText(.body)
                .fixedSize(horizontal: false, vertical: true)
            // The document camera first — flattened, squared and without the
            // card landing in the photo library (parity audit #92); the
            // library is the fallback, and the only way in without a camera.
            if DocumentCameraView.isAvailable {
                Button {
                    Haptic.light()
                    showingCamera = true
                } label: {
                    HStack(spacing: CavnarSpace.xs) {
                        Image(systemName: "doc.viewfinder").accessibilityHidden(true)
                        Text("Scan a recipe card")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isScanning))
                .disabled(viewModel.isScanning)
                PhotosPicker(selection: $pickerItem, matching: .images) {
                    HStack(spacing: CavnarSpace.xs) {
                        Image(systemName: "photo.on.rectangle").accessibilityHidden(true)
                        Text("Choose a photo instead")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(viewModel.isScanning)
            } else {
                PhotosPicker(selection: $pickerItem, matching: .images) {
                    HStack(spacing: CavnarSpace.xs) {
                        Image(systemName: "camera.viewfinder").accessibilityHidden(true)
                        Text("Photograph a recipe card")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isScanning))
                .disabled(viewModel.isScanning)
            }
        }
        .cavnarCard()
    }

    /// `pinned`: this draft's answer is in the pinned bar, not on the card.
    private func draftCard(_ draft: RecipeDraft, pinned: Bool) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Text(Self.dishName(draft))
                    .cavnarText(.lead)
                // Drafted from your ingredient list, not read off a card —
                // an estimate until you accept it (H6).
                if draft.isEstimate == true {
                    ClaimKindTag(kind: "estimate")
                }
            }
            if let note = draft.note, !note.isEmpty {
                Text(note).cavnarText(.secondary)
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
            // The first few lines, the rest behind "+N more"; changing a
            // line is the web's (re-audit F17). Accept and Not right stay.
            // A line marked for a look never hides behind the fold.
            let flaggedBelow = draft.lines.dropFirst(Self.linesShown).contains { $0.mark != nil || $0.unitUnconverted }
            let linesOpen = flaggedBelow || linesExpanded.contains(draft.id)
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(draft.lines.prefix(linesOpen ? draft.lines.count : Self.linesShown))) { line in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 8) {
                            Text(line.name).cavnarText(.secondary)
                            if line.isBatch {
                                Text("per batch").cavnarText(.caption)
                            }
                            Spacer(minLength: CavnarSpace.xxs)
                            Text(Self.qty(line.qty) + (line.unit.map { " \($0)" } ?? ""))
                                .cavnarText(.figureS, color: line.mark == .doubt || line.unitUnconverted ? Color.cavnarAmber : Color.cavnarInk)
                            if line.unitUnconverted {
                                Text("unit?").cavnarText(.caption, color: .cavnarAmber)
                                    .accessibilityLabel("The unit didn't convert — skipped on accept")
                            } else {
                                switch line.mark {
                                case .doubt:
                                    Text("?").cavnarText(.caption, color: .cavnarAmber)
                                        .accessibilityLabel("Unsure — check this amount")
                                case .check:
                                    Text("check").cavnarText(.caption)
                                        .accessibilityLabel("Worth a check")
                                case nil:
                                    EmptyView()
                                }
                            }
                        }
                        // What the card itself said, when conversion changed it.
                        if let card = line.cardLine {
                            CavnarMixedText(card, role: .caption)
                        }
                    }
                }
                if draft.lines.count > Self.linesShown && !flaggedBelow {
                    CavnarMoreToggle(hiddenCount: draft.lines.count - Self.linesShown,
                                     isExpanded: Binding(
                                        get: { linesExpanded.contains(draft.id) },
                                        set: { if $0 { linesExpanded.insert(draft.id) } else { linesExpanded.remove(draft.id) } }))
                }
            }
            // A batch card never said how many plates it makes: its lines
            // are refused as one plate's until the yield is given (H6).
            if draft.requiresYield {
                VStack(alignment: .leading, spacing: 6) {
                    Text("This card is a batch. How many plates does it make?")
                        .cavnarText(.label, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    TextField("Plates", text: Binding(
                        get: { yieldText[draft.id] ?? "" },
                        set: { yieldText[draft.id] = $0 }))
                        .keyboardType(.decimalPad)
                        .cavnarTextFieldStyle()
                        .font(.cavnar(.figureS))
                        .frame(maxWidth: 140, alignment: .leading)
                        .accessibilityLabel("Plates this card makes")
                }
            }
            if !pinned {
                HStack(spacing: CavnarSpace.s) {
                    answerButtons(draft)
                }
            }
        }
        .cavnarCard(.ai)
    }

    /// Not right · Accept recipe — on the card, or pinned for the first.
    @ViewBuilder
    private func answerButtons(_ draft: RecipeDraft) -> some View {
        let typedYield = Self.parsedYield(yieldText[draft.id])
        let blockedOnYield = draft.requiresYield && typedYield == nil
        Button {
            Haptic.light()
            // Deletes the draft — after a short Undo (re-audit F12).
            viewModel.rejectWithUndo(draft)
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

    /// The dish's name, never an internal id ("Dish #412" was one).
    static func dishName(_ draft: RecipeDraft) -> String {
        if let name = draft.menuItemName?.trimmingCharacters(in: .whitespaces), !name.isEmpty { return name }
        return "A dish without a name"
    }

    /// "Mac and cheese draft removed · Undo" while the delete waits.
    private func undoRow(_ draft: RecipeDraft) -> some View {
        HStack(spacing: CavnarSpace.s) {
            CavnarMixedText("\(Self.dishName(draft)) draft removed", role: .label)
            Spacer(minLength: CavnarSpace.xs)
            Button {
                Haptic.light()
                viewModel.undoReject()
            } label: {
                Text("Undo").frame(minWidth: 88)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .accessibilityHint("Brings the draft back")
        }
        .frame(minHeight: 44)
        .cavnarCard()
    }

    private static func qty(_ q: Double) -> String {
        q == q.rounded() ? String(Int(q)) : String(format: "%.2f", q).replacingOccurrences(of: #"0+$"#, with: "", options: .regularExpression)
    }
}
