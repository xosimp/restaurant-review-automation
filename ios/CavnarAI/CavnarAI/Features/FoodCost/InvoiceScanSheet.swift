import ImageIO
import SwiftUI
import PhotosUI
import Observation

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

    var id: Int { index }

    enum CodingKeys: String, CodingKey {
        case index, description, quantity, unit, note, selected
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
    let ingredients: [InvoiceIngredient]
    let appliedAt: String?
    let duplicate: Bool?

    enum CodingKeys: String, CodingKey {
        case id, supplier, lines, ingredients, duplicate
        case invoiceDate = "invoice_date"
        case totalCheck = "total_check"
        case appliedAt = "applied_at"
    }
}

@Observable
@MainActor
final class InvoiceScanViewModel {
    /// The owner's working copy of each line: include it, which ingredient,
    /// what cost. Seeded from the server's proposal.
    struct Choice {
        var include: Bool
        var ingredientId: Int?
        var cost: String
    }

    var invoice: ScannedInvoice?
    var choices: [Int: Choice] = [:]
    var isScanning = false
    var isApplying = false
    var errorMessage: String?
    var appliedCount: Int?

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
        isScanning = true
        defer { isScanning = false }
        do {
            guard let raw = try await item.loadTransferable(type: Data.self),
                  let jpeg = await Task.detached(priority: .userInitiated, operation: {
                      Self.downscaledJPEG(raw)
                  }).value else {
                errorMessage = "That photo couldn't be read. Try another."
                return
            }
            let r: ScanResponse = try await client.upload("/mobile/api/food-cost/invoices",
                                                          fileData: jpeg, filename: "invoice.jpg",
                                                          mimeType: "image/jpeg")
            guard r.ok, let inv = r.invoice else {
                errorMessage = r.error ?? "The invoice couldn't be read."
                return
            }
            invoice = inv
            choices = Dictionary(uniqueKeysWithValues: inv.lines.map { line in
                (line.index, Choice(include: line.selected, ingredientId: line.ingredientId,
                                    cost: line.proposedCost.map { Self.costString($0) } ?? ""))
            })
            await Haptic.success()
        } catch is CancellationError {
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func apply() async {
        // Once is enough. After a success the button stayed, and a second
        // tap answered with a red "already applied" under the green success
        // (CLIENT-60); a new scan clears appliedCount.
        guard let inv = invoice, !isApplying, appliedCount == nil, inv.appliedAt == nil else { return }
        errorMessage = nil
        var lines: [ApplyBody.Line] = []
        for line in inv.lines {
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
    @State private var viewModel = InvoiceScanViewModel()
    @State private var pickerItem: PhotosPickerItem?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    intro
                    if viewModel.isScanning {
                        VStack(alignment: .leading, spacing: 10) {
                            CavnarShimmerText(text: "Reading the invoice…")
                            CavnarSkeletonLines(widths: [1.0, 0.86, 0.7])
                        }
                        .cavnarCard()
                    }
                    if let error = viewModel.errorMessage {
                        Text(error)
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let applied = viewModel.appliedCount {
                        Text("\(applied) ingredient cost\(applied == 1 ? "" : "s") updated. Plate costs and margins now use them.")
                            .font(.cavnarBody(15, weight: 600))
                            .foregroundStyle(Color.cavnarGreen)
                            .fixedSize(horizontal: false, vertical: true)
                    } else if let inv = viewModel.invoice {
                        invoiceCard(inv)
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Scan invoice")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Scan invoice")
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
            Text("KEEP COSTS CURRENT")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            Text("Photograph a supplier invoice. Cavnar reads the prices and proposes updates — nothing changes until you confirm each line.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            PhotosPicker(selection: $pickerItem, matching: .images) {
                HStack(spacing: 8) {
                    Image(systemName: "doc.text.viewfinder").font(.system(size: 13, weight: .semibold))
                    Text(viewModel.invoice == nil ? "Choose invoice photo" : "Scan another")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isScanning))
            .disabled(viewModel.isScanning)
        }
        .cavnarCard()
    }

    private func invoiceCard(_ inv: ScannedInvoice) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            // The invoice's own date, M/D/YY — it used to print as sent,
            // 2026-09-18 (CLIENT-45).
            Text(([inv.supplier ?? "Supplier not read", inv.invoiceDate.map(CavnarDate.mdy)].compactMap { $0 })
                .joined(separator: " · ").uppercased())
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarInk3)
            if let tc = inv.totalCheck, !tc.plausible {
                HomeMixedText.make("Lines add up to \(Self.money(tc.linesSum)) against a total of \(Self.money(tc.invoiceTotal)) — check them against the paper.",
                                   size: 13.5, weight: 500, color: .cavnarAmber)
            }
            if inv.appliedAt != nil {
                Text("This invoice was already applied.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(inv.lines.enumerated()), id: \.element.id) { i, line in
                        lineRow(line, ingredients: inv.ingredients)
                        if i < inv.lines.count - 1 {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                        }
                    }
                }
                Button {
                    Task { await viewModel.apply() }
                } label: {
                    Group {
                        if viewModel.isApplying {
                            CavnarShimmerText(text: "Updating…")
                        } else {
                            Text("Update ticked costs")
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isApplying))
                .disabled(viewModel.isApplying)
            }
        }
        .cavnarCard()
    }

    private func lineRow(_ line: InvoiceLine, ingredients: [InvoiceIngredient]) -> some View {
        let binding = Binding<InvoiceScanViewModel.Choice>(
            get: { viewModel.choices[line.index] ?? .init(include: false, ingredientId: nil, cost: "") },
            set: { viewModel.choices[line.index] = $0 })
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 10) {
                Button {
                    Haptic.light()
                    binding.wrappedValue.include.toggle()
                } label: {
                    Image(systemName: binding.wrappedValue.include ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 20))
                        .foregroundStyle(binding.wrappedValue.include ? Color.cavnarEmber : Color.cavnarInk3)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(binding.wrappedValue.include ? "Included" : "Not included")
                VStack(alignment: .leading, spacing: 3) {
                    Text(line.description).font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarInk)
                    HomeMixedText.make(detail(line), size: 12.5, weight: 500, color: .cavnarInk3)
                    if let note = line.note {
                        Text(note).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            HStack(spacing: 10) {
                Picker("Ingredient", selection: Binding(
                    get: { binding.wrappedValue.ingredientId },
                    set: { binding.wrappedValue.ingredientId = $0 })) {
                    Text("Pick ingredient").tag(Int?.none)
                    ForEach(ingredients) { ing in
                        Text(ing.unit.map { "\(ing.name) (\($0))" } ?? ing.name).tag(Int?.some(ing.id))
                    }
                }
                .pickerStyle(.menu)
                .tint(Color.cavnarEmber2)
                Spacer(minLength: 6)
                TextField("Cost", text: Binding(
                    get: { binding.wrappedValue.cost },
                    set: { binding.wrappedValue.cost = $0 }))
                    .keyboardType(.decimalPad)
                    .multilineTextAlignment(.trailing)
                    .font(.cavnarNumber(15, weight: 600))
                    .frame(width: 92)
                    .padding(.vertical, 6).padding(.horizontal, 8)
                    .background(RoundedRectangle(cornerRadius: 8).stroke(Color.cavnarPaper3, lineWidth: 1))
            }
            .padding(.leading, 30)
        }
        .padding(.vertical, 11)
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
