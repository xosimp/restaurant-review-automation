import ImageIO
import SwiftUI
import PhotosUI
import Observation
import UniformTypeIdentifiers

/// One line of a scanned invoice, as the server proposes it (invoices.py).
/// The proposal is only a starting point — the owner can change the
/// ingredient, the cost, or whether the line is applied at all.
struct InvoiceLine: Decodable, Identifiable {
    let index: Int
    let description: String
    let quantity: Double?
    let unit: String?
    let unitPrice: Double?
    let lineTotal: Double?
    let ingredientId: Int?
    let ingredientName: String?
    let currentCost: Double?
    let proposedCost: Double?
    let changePct: Double?
    let selected: Bool
    let note: String?
    /// Already written — by the trusted-supplier rule, or an earlier apply.
    let applied: Bool?
    /// Whether the reading passed both checks (invoices.py): its arithmetic
    /// agreed and there was a current cost to compare with. Nil when the
    /// server did not say. Shown on each line, as the web does.
    let verified: Bool?
    /// How the line found its ingredient (memory round, 9/29/26:
    /// invoices.propose): "your_match" — the one the owner picked for this
    /// line before — or "name". Nil when unmatched or on an older server.
    var matchedBy: String? = nil

    var id: Int { index }

    /// "Matched as you did last time" — only for the owner's own match.
    var matchNote: String? { matchedBy == "your_match" ? "Matched as you did last time" : nil }

    enum CodingKeys: String, CodingKey {
        case matchedBy = "matched_by"
        case index, description, quantity, unit, note, selected, applied, verified
        case unitPrice = "unit_price"
        case lineTotal = "line_total"
        case ingredientId = "ingredient_id"
        case ingredientName = "ingredient_name"
        case currentCost = "current_cost"
        case proposedCost = "proposed_cost"
        case changePct = "change_pct"
    }
}

struct InvoiceIngredient: Decodable, Identifiable, Hashable {
    let id: Int
    let name: String
    let unit: String?
}

struct InvoiceTotalCheck: Decodable {
    let linesSum: Double
    let invoiceTotal: Double
    let plausible: Bool

    enum CodingKeys: String, CodingKey {
        case plausible
        case linesSum = "lines_sum"
        case invoiceTotal = "invoice_total"
    }
}

struct ScannedInvoice: Decodable {
    let id: Int
    let supplier: String?
    let invoiceDate: String?
    let lines: [InvoiceLine]
    let totalCheck: InvoiceTotalCheck?
    /// var: a line can become a new ingredient on the spot.
    var ingredients: [InvoiceIngredient]
    let appliedAt: String?
    let duplicate: Bool?
    /// The rule applied the lines it could check on its own; the rest
    /// (flagged, unverified) still wait for the owner (M-4).
    let awaitingOwner: Bool?
    let autoAppliedCount: Int?

    /// Whether the owner still has lines to decide on.
    var isOpen: Bool { appliedAt == nil || awaitingOwner == true }

    enum CodingKeys: String, CodingKey {
        case id, supplier, lines, ingredients, duplicate
        case invoiceDate = "invoice_date"
        case totalCheck = "total_check"
        case appliedAt = "applied_at"
        case awaitingOwner = "awaiting_owner"
        case autoAppliedCount = "auto_applied_count"
    }
}

/// A scanned invoice still waiting on the owner (invoices.list_imports,
/// `pending`) — listed so one can be reopened after the sheet was closed.
struct PendingInvoice: Decodable, Identifiable {
    let id: Int
    let supplier: String?
    let invoiceDate: String?
    let waiting: Int?
    let pending: Bool?

    enum CodingKeys: String, CodingKey {
        case id, supplier, waiting, pending
        case invoiceDate = "invoice_date"
    }
}

@Observable
@MainActor
final class InvoiceScanViewModel {
    /// The owner's working copy of each line: include it, which ingredient,
    /// what cost. Seeded from the server's proposal.
    struct Choice: Equatable {
        var include: Bool
        var ingredientId: Int?
        var cost: String
    }

    var invoice: ScannedInvoice?
    var choices: [Int: Choice] = [:]
    /// The proposal as it was put on screen — what `choices` started as.
    private(set) var seededChoices: [Int: Choice] = [:]

    /// Lines ticked, unticked or re-costed and not yet sent (re-audit F2):
    /// closing the sheet would lose them, so it asks first.
    var hasUnsavedEdits: Bool {
        guard invoice != nil, appliedCount == nil, !isApplying else { return false }
        return choices != seededChoices
    }
    var isScanning = false
    var isApplying = false
    var errorMessage: String?
    var appliedCount: Int?
    /// Scanned invoices still waiting on the owner, newest first.
    var pending: [PendingInvoice] = []
    var isOpening = false
    /// The line whose "New ingredient from this line" is being created.
    var creatingFor: Int?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct ScanResponse: Decodable {
        let ok: Bool
        let invoice: ScannedInvoice?
        let error: String?
    }

    private struct ApplyResponse: Decodable {
        struct Updated: Decodable { let name: String }
        let ok: Bool
        let updated: [Updated]?
        let error: String?
    }

    private struct ApplyBody: Encodable {
        struct Line: Encodable {
            let index: Int
            let ingredientId: Int
            let unitCost: Double
            enum CodingKeys: String, CodingKey {
                case index
                case ingredientId = "ingredient_id"
                case unitCost = "unit_cost"
            }
        }
        let lines: [Line]
    }

    func scan(_ item: PhotosPickerItem) async {
        errorMessage = nil
        appliedCount = nil
        extraPagesNote = nil
        isScanning = true
        defer { isScanning = false }
        do {
            guard let raw = try await item.loadTransferable(type: Data.self) else {
                errorMessage = "That photo couldn't be read. Try another."
                return
            }
            await upload(raw)
        } catch is CancellationError {
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// The files Files can hand over: a supplier's emailed PDF, or a photo
    /// saved there (parity audit #43; the route reads both).
    static let importTypes: [UTType] = [.pdf, .image]

    /// The server's limit on a PDF (invoices.MAX_PDF_BYTES is 4.5 MB).
    static let maxPDFBytes = Int(4.5 * 1024 * 1024)

    /// An invoice chosen in Files — a PDF goes up as the document it is
    /// (the server reads it page by page); an image goes up like a photo.
    func scan(file url: URL) async {
        errorMessage = nil
        appliedCount = nil
        extraPagesNote = nil
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard let data = try? Data(contentsOf: url) else {
            errorMessage = "That file couldn\u{2019}t be opened. Try another."
            return
        }
        isScanning = true
        defer { isScanning = false }
        let type = UTType(filenameExtension: url.pathExtension.lowercased())
        if type?.conforms(to: .pdf) == true || data.starts(with: Data("%PDF".utf8)) {
            guard data.count <= Self.maxPDFBytes else {
                errorMessage = "That PDF is over 4.5 MB. Send the invoice pages on their own, or scan them with the camera."
                return
            }
            extraPagesNote = "Reading every page of the PDF."
            await send(data, filename: "invoice.pdf", mimeType: "application/pdf")
            return
        }
        await upload(data)
    }

    /// Pages from the document camera (Friction audit #28). One page goes
    /// up as a photo; several go up together as one PDF, which the server
    /// reads page by page (invoices.py takes a PDF as a document) — only
    /// the first page used to be read, and a two-page invoice lost the
    /// second page's lines.
    func scan(pages: [UIImage]) async {
        guard let first = pages.first else { return }
        errorMessage = nil
        appliedCount = nil
        extraPagesNote = nil
        isScanning = true
        defer { isScanning = false }
        if pages.count == 1 {
            guard let raw = first.jpegData(compressionQuality: 0.9) else {
                errorMessage = "That scan couldn't be read. Try again."
                return
            }
            await upload(raw)
            return
        }
        guard let pdf = await Task.detached(priority: .userInitiated, operation: {
                  Self.pagesPDF(pages)
              }).value else {
            errorMessage = "Those \(pages.count) pages are too much to send at once. Scan a few pages at a time."
            return
        }
        extraPagesNote = "Reading all \(pages.count) pages."
        await send(pdf, filename: "invoice.pdf", mimeType: "application/pdf")
    }

    /// Set while a multi-page camera scan is read.
    var extraPagesNote: String?

    private func upload(_ raw: Data) async {
        guard let jpeg = await Task.detached(priority: .userInitiated, operation: {
                  Self.downscaledJPEG(raw)
              }).value else {
            errorMessage = "That photo couldn't be read. Try another."
            return
        }
        await send(jpeg, filename: "invoice.jpg", mimeType: "image/jpeg")
    }

    private func send(_ data: Data, filename: String, mimeType: String) async {
        do {
            // Read as a job this screen polls (`async`, AI cost audit 10/7/26
            // #57): the upload returns at once and the read runs off the
            // server's request threads. An older server answers the scan
            // itself, which resolveAIJob passes straight through.
            let started: APIClient.AIJobAnswer<ScanResponse> = try await client.upload(
                "/mobile/api/food-cost/invoices", fileData: data, filename: filename, mimeType: mimeType,
                query: ["async": "1"])
            let r: ScanResponse = try await client.resolveAIJob(started)
            guard r.ok, let inv = r.invoice else {
                errorMessage = r.error ?? "The invoice couldn't be read."
                return
            }
            show(inv)
            extraPagesNote = nil
            await Haptic.success()
            await loadPending()
        } catch is CancellationError {
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Puts an invoice on screen with the server's proposal as the working copy.
    private func show(_ inv: ScannedInvoice) {
        invoice = inv
        appliedCount = nil
        // A line with no ingredient is matched on the web (iOS readability
        // round, 10/8/26): the phone approves matched lines, so an unmatched
        // one is never ticked here — it would only block the update.
        choices = Dictionary(uniqueKeysWithValues: inv.lines.map { line in
            (line.index, Choice(include: line.selected && line.applied != true && line.ingredientId != nil,
                                ingredientId: line.ingredientId,
                                cost: line.proposedCost.map { Self.costString($0) } ?? ""))
        })
        seededChoices = choices
    }

    // MARK: - Pending invoices (U2-2)

    private struct ListResponse: Decodable { let ok: Bool; let invoices: [PendingInvoice] }

    /// GET /food-cost/invoices — what the web lists as "Waiting on you".
    func loadPending() async {
        do {
            let r: ListResponse = try await client.send("/mobile/api/food-cost/invoices", hapticOnError: false)
            pending = r.invoices.filter { $0.pending == true }
        } catch {
            // Secondary: the scanner works without the list.
        }
    }

    /// GET /food-cost/invoices/<id> — reopens one invoice's lines to
    /// confirm and apply (a pending row, or the "invoice/<id>" link).
    func open(_ id: Int) async {
        errorMessage = nil
        isOpening = true
        defer { isOpening = false }
        do {
            let r: ScanResponse = try await client.send("/mobile/api/food-cost/invoices/\(id)")
            guard r.ok, let inv = r.invoice else {
                errorMessage = r.error ?? "That invoice isn\u{2019}t here any more."
                return
            }
            show(inv)
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t open that invoice."
        }
    }

    // MARK: - New ingredient from a line (U2-23)

    private struct NewIngredientBody: Encodable {
        let name: String
        let unit: String
        let unitCost: Double?
        enum CodingKeys: String, CodingKey { case name, unit; case unitCost = "unit_cost" }
    }
    private struct NewIngredientResponse: Decodable {
        let ok: Bool
        let ingredient: InvoiceIngredient?
        let error: String?
    }

    /// A line that matches no ingredient becomes one, prefilled with its
    /// description, unit and the cost the invoice implies — the web's
    /// "+ New ingredient from this line" (POST /food-cost/ingredients).
    func createIngredient(from line: InvoiceLine) async {
        guard creatingFor == nil else { return }
        errorMessage = nil
        creatingFor = line.index
        defer { creatingFor = nil }
        let typed = choices[line.index].flatMap {
            FoodCostQuickEntryViewModel.parsedPrice($0.cost.replacingOccurrences(of: "$", with: ""))
        }
        let cost = (typed ?? 0) > 0 ? typed : line.unitPrice
        do {
            let r: NewIngredientResponse = try await client.send(
                "/mobile/api/food-cost/ingredients", method: .post,
                body: NewIngredientBody(name: line.description, unit: line.unit ?? "", unitCost: cost),
                retryTransient: false)
            guard r.ok, let ing = r.ingredient else {
                errorMessage = r.error ?? "Couldn\u{2019}t add that ingredient."
                return
            }
            invoice?.ingredients.append(ing)
            var c = choices[line.index] ?? Choice(include: true, ingredientId: nil, cost: "")
            c.ingredientId = ing.id
            c.include = true
            if c.cost.isEmpty, let cost { c.cost = Self.costString(cost) }
            choices[line.index] = c
            await Haptic.success()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t add that ingredient."
        }
    }

    func apply() async {
        // Once is enough. After a success the button stayed, and a second
        // tap answered with a red "already applied" under the green success
        // (CLIENT-60); a new scan clears appliedCount.
        guard let inv = invoice, !isApplying, appliedCount == nil, inv.isOpen else { return }
        errorMessage = nil
        var lines: [ApplyBody.Line] = []
        for line in inv.lines where line.applied != true {
            guard let c = choices[line.index], c.include else { continue }
            guard let ing = c.ingredientId,
                  let cost = FoodCostQuickEntryViewModel.parsedPrice(c.cost.replacingOccurrences(of: "$", with: "")),
                  cost > 0 else {
                errorMessage = "“\(line.description)” needs an ingredient and a cost, or untick it."
                return
            }
            lines.append(.init(index: line.index, ingredientId: ing, unitCost: cost))
        }
        guard !lines.isEmpty else {
            errorMessage = "Tick at least one line to update."
            return
        }
        isApplying = true
        defer { isApplying = false }
        do {
            let r: ApplyResponse = try await client.send(
                "/mobile/api/food-cost/invoices/\(inv.id)/apply", method: .post,
                body: ApplyBody(lines: lines), retryTransient: false)
            if r.ok {
                appliedCount = r.updated?.count ?? lines.count
                await Haptic.success()
                await loadPending()
            } else {
                errorMessage = r.error ?? "Couldn't update those costs."
            }
        } catch is CancellationError {
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Invoices are read, not admired: 2000px on the long side keeps every
    /// printed digit legible and the upload well under the server's 4.5 MB.
    ///
    /// Sampled straight from the file with ImageIO, which decodes only at
    /// the target size. UIImage(data:) on the main actor decoded a 48 MP
    /// photo to a ~190 MB bitmap first and froze the sheet while it did
    /// (CLIENT-39); `scan` now runs this on a background task.
    nonisolated static func downscaledJPEG(_ data: Data, maxSide: CGFloat = 2000) -> Data? {
        // nonisolated: pure work on the bytes it is handed, safe off-main.
        let noCache = [kCGImageSourceShouldCache: false] as CFDictionary
        guard let source = CGImageSourceCreateWithData(data as CFData, noCache),
              let options = thumbnailOptions(source, maxSide: maxSide),
              let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options)
        else { return nil }
        return UIImage(cgImage: cgImage).jpegData(compressionQuality: 0.8)
    }

    /// Every camera page in one PDF, under the server's 4.5 MB (invoices
    /// .MAX_PDF_BYTES): each page a JPEG at most `maxSide` on its long edge,
    /// stepping down until the whole file fits. Nil when even the smallest
    /// step is too big — the owner is asked to scan fewer pages.
    nonisolated static func pagesPDF(_ pages: [UIImage]) -> Data? {
        let limit = Int(4.4 * 1024 * 1024)
        for (maxSide, quality) in [(1800.0, 0.75), (1400.0, 0.65), (1100.0, 0.55), (900.0, 0.5)] {
            var images: [(CGImage, CGSize)] = []
            for page in pages {
                guard let raw = page.jpegData(compressionQuality: 0.9),
                      let small = downscaledJPEG(raw, maxSide: CGFloat(maxSide)),
                      let decoded = UIImage(data: small)?.jpegData(compressionQuality: CGFloat(quality)),
                      let provider = CGDataProvider(data: decoded as CFData),
                      let cg = CGImage(jpegDataProviderSource: provider, decode: nil,
                                       shouldInterpolate: true, intent: .defaultIntent)
                else { return nil }
                images.append((cg, CGSize(width: cg.width, height: cg.height)))
            }
            let data = NSMutableData()
            guard let consumer = CGDataConsumer(data: data as CFMutableData) else { return nil }
            var box = CGRect(x: 0, y: 0, width: 612, height: 792)
            guard let ctx = CGContext(consumer: consumer, mediaBox: &box, nil) else { return nil }
            for (cg, size) in images {
                // Points at 144 dpi: legible, and the page keeps its shape.
                var rect = CGRect(x: 0, y: 0, width: size.width / 2, height: size.height / 2)
                ctx.beginPage(mediaBox: &rect)
                ctx.draw(cg, in: rect)
                ctx.endPage()
            }
            ctx.closePDF()
            if data.length <= limit { return data as Data }
        }
        return nil
    }

    /// Thumbnail options for `source`: at most `maxSide` on the long edge,
    /// never upscaled (a small photo keeps its own size), EXIF orientation
    /// applied.
    private nonisolated static func thumbnailOptions(_ source: CGImageSource, maxSide: CGFloat) -> CFDictionary? {
        guard let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = props[kCGImagePropertyPixelWidth] as? CGFloat,
              let height = props[kCGImagePropertyPixelHeight] as? CGFloat
        else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: min(maxSide, max(width, height)),
        ]
        return options as CFDictionary
    }

    static func costString(_ v: Double) -> String {
        String(format: v < 1 ? "%.4f" : "%.2f", v)
    }
}

struct InvoiceScanSheet: View {
    /// Open straight onto the camera — the Scan invoice quick action, the
    /// App Shortcut and Food Cost's action row (Friction audit #28).
    var startWithCamera: Bool = false
    /// Open on this scanned invoice's lines (a pending invoice, or the
    /// action queue's "invoice/<id>").
    var invoiceId: Int? = nil
    @State private var viewModel = InvoiceScanViewModel()
    @State private var pickerItem: PhotosPickerItem?
    @State private var showingCamera = false
    @State private var didAutoOpenCamera = false
    /// Files — a supplier's emailed PDF (parity audit #43).
    @State private var importingFile = false
    /// Done pressed with lines changed and not sent (re-audit F2).
    @State private var confirmingLeave = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    intro
                    pendingList
                    if viewModel.isOpening {
                        CavnarWorkingLine().padding(.vertical, 8)
                    }
                    if viewModel.isScanning {
                        VStack(alignment: .leading, spacing: 10) {
                            CavnarShimmerText(text: "Reading the invoice…")
                            CavnarSkeletonLines(widths: [1.0, 0.86, 0.7])
                        }
                        .cavnarCard()
                    }
                    if let error = viewModel.errorMessage {
                        Text(error)
                            .cavnarText(.body, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let applied = viewModel.appliedCount {
                        CavnarMixedText("\(applied) ingredient cost\(applied == 1 ? "" : "s") updated. Plate costs and margins now use them.",
                                        role: .body, color: .cavnarGreen)
                    } else if let inv = viewModel.invoice {
                        invoiceCard(inv)
                    }
                }
                .padding(CavnarSpace.gutter)
            }
            // The update in thumb reach, not under every line.
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if viewModel.appliedCount == nil, let inv = viewModel.invoice, inv.isOpen,
                   inv.lines.contains(where: { $0.applied != true && $0.ingredientId != nil }) {
                    CavnarPinnedBar { applyButton }
                }
            }
            .cavnarModuleBackground()
            .navigationTitle("Scan invoice")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Scan invoice")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        if viewModel.hasUnsavedEdits { confirmingLeave = true } else { dismiss() }
                    } label: {
                        Text("Done").cavnarText(.label, color: .cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            // Ticked lines and typed costs are never lost to a swipe: with
            // edits on screen the sheet closes only through Done, which asks
            // (re-audit F2, as the count sheet does).
            .interactiveDismissDisabled(viewModel.hasUnsavedEdits)
            .confirmationDialog("Leave without updating costs?", isPresented: $confirmingLeave,
                                titleVisibility: .visible) {
                Button("Discard changes", role: .destructive) { dismiss() }
                Button("Keep editing", role: .cancel) {}
            } message: {
                Text("The lines you ticked and the costs you changed aren\u{2019}t saved. The invoice stays in Waiting on you.")
            }
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
            .fileImporter(isPresented: $importingFile, allowedContentTypes: InvoiceScanViewModel.importTypes,
                          allowsMultipleSelection: false) { result in
                guard case .success(let urls) = result, let url = urls.first else { return }
                Task { await viewModel.scan(file: url) }
            }
            // After the sheet's own presentation has settled: a cover asked
            // for in the same onAppear is dropped by SwiftUI while the sheet
            // is still animating in, and the quick action landed on the sheet
            // with no camera (F3-18).
            .task {
                if let invoiceId { await viewModel.open(invoiceId) }
                await viewModel.loadPending()
            }
            .task {
                guard invoiceId == nil, startWithCamera, !didAutoOpenCamera, DocumentCameraView.isAvailable else { return }
                didAutoOpenCamera = true
                try? await Task.sleep(for: .milliseconds(600))
                showingCamera = true
            }
        }
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            Text("Photograph a supplier invoice. Cavnar AI reads the prices and proposes updates — nothing changes until you confirm them.")
                .cavnarText(.body)
                .fixedSize(horizontal: false, vertical: true)
            // The camera is the way in; the photo library is the fallback
            // (and the only way on a device without a camera).
            if DocumentCameraView.isAvailable {
                Button {
                    Haptic.light()
                    showingCamera = true
                } label: {
                    HStack(spacing: CavnarSpace.xs) {
                        Image(systemName: "camera.viewfinder").accessibilityHidden(true)
                        Text(viewModel.invoice == nil ? "Scan with camera" : "Scan another")
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
                        Image(systemName: "doc.text.viewfinder").accessibilityHidden(true)
                        Text(viewModel.invoice == nil ? "Choose invoice photo" : "Scan another")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isScanning))
                .disabled(viewModel.isScanning)
            }
            // Supplier invoices arrive as emailed PDFs: save to Files, pick
            // here (parity audit #43).
            Button {
                Haptic.light()
                importingFile = true
            } label: {
                HStack(spacing: CavnarSpace.xs) {
                    Image(systemName: "doc.richtext").accessibilityHidden(true)
                    Text("Choose a PDF or file")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(viewModel.isScanning)
            if let note = viewModel.extraPagesNote {
                CavnarMixedText(note, role: .secondary, color: .cavnarAmber)
            }
        }
        .cavnarCard()
    }

    /// "Waiting on you" — scanned invoices not yet applied, each one tap
    /// from its lines (the web's fc2LoadPendingInvoices). The one on screen
    /// is left out.
    @ViewBuilder
    private var pendingList: some View {
        let rows = viewModel.pending.filter { $0.id != viewModel.invoice?.id }
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("Waiting on you \u{00B7} \(rows.count) scanned invoice\(rows.count == 1 ? "" : "s")")
                ForEach(rows) { row in
                    HStack(spacing: CavnarSpace.s) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(row.supplier ?? "Supplier not read").cavnarText(.label)
                            CavnarMixedText(pendingDetail(row), role: .secondary)
                        }
                        Spacer(minLength: CavnarSpace.xs)
                        Button {
                            Haptic.light()
                            Task { await viewModel.open(row.id) }
                        } label: {
                            Text("Open").cavnarText(.label, color: .cavnarEmber2).cavnarHitTarget()
                        }
                        .buttonStyle(.plain)
                        .disabled(viewModel.isOpening || viewModel.isScanning)
                    }
                }
            }
            .cavnarCard()
        }
    }

    private func pendingDetail(_ row: PendingInvoice) -> String {
        var parts: [String] = []
        if let d = row.invoiceDate { parts.append(CavnarDate.mdy(d)) }
        let n = row.waiting ?? 0
        parts.append("\(n) line\(n == 1 ? "" : "s") to check")
        return parts.joined(separator: " · ")
    }

    /// The invoice's matched lines to approve, each ticked or not with its
    /// cost open to change; lines Cavnar AI could not match to an
    /// ingredient are matched on the web (iOS readability round, 10/8/26).
    private func invoiceCard(_ inv: ScannedInvoice) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            // The invoice's own date, M/D/YY — it used to print as sent,
            // 2026-09-18 (CLIENT-45).
            Text(([inv.supplier ?? "Supplier not read", inv.invoiceDate.map(CavnarDate.mdy)].compactMap { $0 })
                .joined(separator: " · "))
                .cavnarText(.lead)
            if let tc = inv.totalCheck, !tc.plausible {
                CavnarMixedText("Lines add up to \(Self.money(tc.linesSum)) against a total of \(Self.money(tc.invoiceTotal)) — check them against the paper.",
                                role: .secondary, color: .cavnarAmber)
            }
            if !inv.isOpen {
                Text("This invoice was already applied.").cavnarText(.body)
            } else {
                if inv.awaitingOwner == true {
                    let n = inv.autoAppliedCount ?? 0
                    CavnarMixedText("Cavnar AI applied \(n) checked line\(n == 1 ? "" : "s") from this trusted supplier. The lines below still need you.",
                                    role: .secondary, color: .cavnarAmber)
                }
                let open = inv.lines.filter { $0.applied != true }
                let matched = open.filter { $0.ingredientId != nil }
                let unmatched = open.filter { $0.ingredientId == nil }
                if !matched.isEmpty {
                    CavnarKicker("Approve matched lines")
                    VStack(spacing: 0) {
                        ForEach(Array(matched.enumerated()), id: \.element.id) { i, line in
                            lineRow(line, ingredients: inv.ingredients)
                            if i < matched.count - 1 {
                                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                            }
                        }
                    }
                }
                if !unmatched.isEmpty {
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        CavnarMixedText("\(unmatched.count) line\(unmatched.count == 1 ? "" : "s") not matched to an ingredient",
                                        role: .label, color: .cavnarAmber)
                        Text(unmatched.prefix(4).map(\.description).joined(separator: ", ")
                             + (unmatched.count > 4 ? " and \(unmatched.count - 4) more" : ""))
                            .cavnarText(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        CavnarWebLinkRow(title: "Match them", subtitle: "Pick or add the ingredient for each line",
                                         path: "inventory/invoices", actionLabel: "Open on the web")
                    }
                    .padding(.top, CavnarSpace.xxs)
                }
            }
        }
        .cavnarCard()
    }

    private var applyButton: some View {
        Button {
            Task { await viewModel.apply() }
        } label: {
            Group {
                if viewModel.isApplying {
                    CavnarShimmerText(text: "Updating…")
                } else {
                    Text("Update selected prices")
                }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isApplying))
        .disabled(viewModel.isApplying)
    }

    private func lineRow(_ line: InvoiceLine, ingredients: [InvoiceIngredient]) -> some View {
        let binding = Binding<InvoiceScanViewModel.Choice>(
            get: { viewModel.choices[line.index] ?? .init(include: false, ingredientId: nil, cost: "") },
            set: { viewModel.choices[line.index] = $0 })
        let ingredient = ingredients.first { $0.id == binding.wrappedValue.ingredientId }
        return VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(alignment: .top, spacing: CavnarSpace.xxs) {
                Button {
                    Haptic.light()
                    binding.wrappedValue.include.toggle()
                } label: {
                    Image(systemName: binding.wrappedValue.include ? "checkmark.circle.fill" : "circle")
                        .font(.cavnar(.figureS))
                        .foregroundStyle(binding.wrappedValue.include ? Color.cavnarEmber : Color.cavnarInk2)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityLabel(binding.wrappedValue.include ? "Included" : "Not included")
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(line.description).cavnarText(.label)
                        // Whether both checks ran and agreed (invoices.py
                        // "verified") — never shown on the phone before.
                        if let verified = line.verified {
                            ClaimKindTag(kind: verified ? "checked" : "unchecked")
                                .accessibilityHint(verified ? "The line adds up and the move is plausible"
                                                   : "Not every check could run on this line. Look before applying.")
                        }
                    }
                    CavnarMixedText(detail(line), role: .secondary)
                    if let match = line.matchNote {
                        HStack(spacing: CavnarSpace.xxs) {
                            Image(systemName: "checkmark.circle").accessibilityHidden(true)
                            Text(match)
                        }
                        .cavnarText(.caption, color: .cavnarGreen)
                    }
                    if let note = line.note {
                        Text(note).cavnarText(.caption, color: .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            // The ingredient it updates — matched by Cavnar AI; re-matching
            // a line is on the web.
            HStack(spacing: CavnarSpace.s) {
                Text(ingredient.map { ing in ing.unit.map { "\(ing.name) (\($0))" } ?? ing.name }
                     ?? line.ingredientName ?? "Matched ingredient")
                    .cavnarText(.secondary)
                    .lineLimit(2)
                Spacer(minLength: CavnarSpace.xxs)
                TextField("Cost", text: Binding(
                    get: { binding.wrappedValue.cost },
                    set: { binding.wrappedValue.cost = $0 }))
                    .keyboardType(.decimalPad)
                    .multilineTextAlignment(.trailing)
                    .font(.cavnar(.figureS))
                    .frame(width: 92, height: 44)
                    .padding(.horizontal, 8)
                    .background(RoundedRectangle(cornerRadius: 8).stroke(Color.cavnarPaper3, lineWidth: 1))
                    .accessibilityLabel("Cost for \(line.description)")
            }
            .padding(.leading, 48)
        }
        .padding(.vertical, CavnarSpace.xs)
    }

    private func detail(_ line: InvoiceLine) -> String {
        var parts: [String] = []
        if let q = line.quantity { parts.append("\(Self.num(q)) \(line.unit ?? "")".trimmingCharacters(in: .whitespaces)) }
        if let p = line.unitPrice { parts.append("@ \(Self.money(p))") }
        if let c = line.currentCost { parts.append("now \(Self.money(c))") }
        return parts.joined(separator: " · ")
    }

    private static func money(_ v: Double) -> String { String(format: "$%.2f", v) }
    private static func num(_ v: Double) -> String {
        v.rounded() == v ? String(Int(v)) : String(format: "%.2f", v)
    }
}
