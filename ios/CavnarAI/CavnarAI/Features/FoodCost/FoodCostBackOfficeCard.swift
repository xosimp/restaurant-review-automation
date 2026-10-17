import SwiftUI
import Observation

/// Back Office on the phone (UI-01, 10/16/26) — "Web explains. iPhone
/// decides." The web's Food Cost page carries Food bought vs sold and the
/// plate costs table; the phone carries each as compact rows in one card:
///
///   1. Food bought vs sold — the four-week figure, "4 weeks to M/D/YY",
///      the line that says it is purchase-based and not food cost, the lag
///      line when the newest invoice is more than a week behind, and any
///      sentence the server adds (`to_confirm`, `coverage`, `why_not`);
///   2. Plate costs — the recipes' cost as a % of what sold, and the three
///      best-selling dishes with their plate cost %;
///   3. the full tables on the web.
///
/// A withheld figure is a dash with the server's reason — never a 0. A read
/// that failed says the server's sentence in the card's place; nothing
/// renders while neither payload is available (a kitchen with no Back
/// Office on file).
struct FoodCostBackOfficeCard: View {
    let viewModel: FoodCostBackOfficeViewModel
    /// The feed's name, from the analytics payload (`food_source.label`).
    var feedLabel = "Back Office"

    var body: some View {
        if viewModel.hasSomething {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                Text("From your \(feedLabel) reports").cavnarText(.headline)
                    .accessibilityAddTraits(.isHeader)
                VStack(alignment: .leading, spacing: CavnarSpace.m) {
                    if let p = viewModel.purchases {
                        purchasesBlock(p)
                    } else if let e = viewModel.purchasesError {
                        errorLine("Food bought vs sold", e)
                    }
                    if viewModel.plates != nil || viewModel.platesError != nil {
                        AccountRowDivider()
                    }
                    if let pl = viewModel.plates {
                        platesBlock(pl)
                    } else if let e = viewModel.platesError {
                        errorLine("Plate costs", e)
                    }
                    CavnarWebLinkRow(title: "See all",
                                     subtitle: "Every month, every supplier and every dish\u{2019}s plate cost",
                                     path: "inventory/plates", actionLabel: "Open on the web")
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .cavnarCard()
            }
        }
    }

    // MARK: - Food bought vs sold

    @ViewBuilder
    private func purchasesBlock(_ p: FoodCostPurchases) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker(p.label ?? "Food bought vs sold", isHeader: false)
            if p.available == false {
                CavnarMixedText(p.whyNot ?? "Nothing on file to read yet.", role: .secondary)
            } else {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                    if let pct = p.window?.pct {
                        Text("\(pct, specifier: "%.1f")%")
                            .cavnarText(.figureL)
                            .minimumScaleFactor(0.85)
                            .lineLimit(1)
                    } else {
                        // Withheld: a dash, never a zero.
                        Text("\u{2014}").cavnarText(.figureL, color: .cavnarInk3)
                            .accessibilityLabel("Not measured")
                    }
                    if let to = p.window?.toMdy {
                        CavnarMixedText("bought vs sold \u{00B7} 4 weeks to \(to)", role: .secondary)
                    }
                    Spacer(minLength: 0)
                }
                .accessibilityElement(children: .combine)
                if p.window?.pct == nil, let why = p.window?.whyNot ?? p.whyNot {
                    CavnarMixedText(why, role: .secondary)
                }
                // Visible, not behind an "i" (UI-14).
                Text("What was paid to food suppliers as a % of food sales \u{2014} purchase-based, not food cost.")
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
                if let lag = p.lagLine {
                    warnLine(lag)
                }
                ForEach(serverLines(p.toConfirm, p.coverage), id: \.self) { line in
                    CavnarMixedText(line, role: .caption)
                }
            }
        }
    }

    // MARK: - Plate costs

    @ViewBuilder
    private func platesBlock(_ pl: FoodCostPlates) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Plate costs", isHeader: false)
            if pl.available == false || (pl.plates ?? []).isEmpty {
                CavnarMixedText(pl.whyNot ?? "No recipes on file to cost yet.", role: .secondary)
            } else {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                    if let pct = pl.summary?.costPct {
                        Text("\(pct, specifier: "%.1f")%").cavnarText(.figureM)
                    } else {
                        Text("\u{2014}").cavnarText(.figureM, color: .cavnarInk3)
                            .accessibilityLabel("Not measured")
                    }
                    if let a = pl.window?.fromMdy, let b = pl.window?.toMdy {
                        CavnarMixedText("plate cost % of what sold \u{00B7} \(a) \u{2013} \(b)", role: .secondary)
                    }
                    Spacer(minLength: 0)
                }
                .accessibilityElement(children: .combine)
                let top = Self.topSellers(pl.plates ?? [])
                ForEach(Array(top.enumerated()), id: \.offset) { _, plate in
                    plateRow(plate)
                }
                ForEach(serverLines(nil, pl.coverage), id: \.self) { line in
                    CavnarMixedText(line, role: .caption)
                }
            }
        }
    }

    private func plateRow(_ plate: FoodCostPlates.Plate) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
            VStack(alignment: .leading, spacing: 2) {
                Text(plate.name).cavnarText(.label)
                    .fixedSize(horizontal: false, vertical: true)
                if let units = plate.units, units > 0 {
                    CavnarMixedText("\(Int(units.rounded())) sold", role: .caption)
                }
            }
            Spacer(minLength: CavnarSpace.xs)
            // An incomplete recipe has no percentage: "at least" its cost.
            if let pct = plate.costPct, plate.complete != false {
                CavnarMixedText(String(format: "%.1f%% plate cost", pct), role: .secondary)
            } else if let cost = plate.cost {
                CavnarMixedText(String(format: "\u{2265} $%.2f a plate", cost), role: .secondary)
            } else {
                Text("\u{2014}").cavnarText(.secondary, color: .cavnarInk3)
            }
        }
        .padding(.vertical, CavnarSpace.xxs)
        .accessibilityElement(children: .combine)
    }

    /// The three dishes that sold most — the rest are on the web.
    static func topSellers(_ plates: [FoodCostPlates.Plate]) -> [FoodCostPlates.Plate] {
        Array(plates.sorted { ($0.units ?? 0) > ($1.units ?? 0) }.prefix(3))
    }

    // MARK: - Helpers

    private func serverLines(_ a: FoodCostSentences?, _ b: FoodCostSentences?) -> [String] {
        var seen = Set<String>()
        return ((a?.lines ?? []) + (b?.lines ?? [])).filter { !$0.isEmpty && seen.insert($0).inserted }
    }

    private func warnLine(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarAmber)
                .accessibilityHidden(true)
            CavnarMixedText(text, role: .secondary, color: .cavnarAmber)
        }
        .accessibilityElement(children: .combine)
    }

    /// A read that failed: the server's sentence where the figure goes.
    private func errorLine(_ title: String, _ message: String) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarKicker(title, isHeader: false)
            CavnarMixedText(message, role: .secondary)
        }
    }
}

// MARK: - Model

/// Reads Food bought vs sold and the plate costs for a Back Office
/// kitchen. Best-effort like the Bar card: a failure says the server's
/// sentence in its block, never takes the screen down.
@Observable
@MainActor
final class FoodCostBackOfficeViewModel {
    private(set) var purchases: FoodCostPurchases?
    private(set) var plates: FoodCostPlates?
    private(set) var purchasesError: String?
    private(set) var platesError: String?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    /// Anything to draw: a figure, a reason it is withheld, or an error.
    var hasSomething: Bool {
        purchases != nil || plates != nil || purchasesError != nil || platesError != nil
    }

    func load() async {
        async let p: Result<FoodCostPurchases, Error> = Self.fetch(client, "/mobile/api/food-cost/purchases")
        async let pl: Result<FoodCostPlates, Error> = Self.fetch(client, "/mobile/api/food-cost/plates")
        let rp = Self.adopt(await p, keeping: purchases)
        purchases = rp.0
        purchasesError = rp.1
        let rpl = Self.adopt(await pl, keeping: plates)
        plates = rpl.0
        platesError = rpl.1
    }

    private static func fetch<T: Decodable>(_ client: APIClient, _ path: String) async -> Result<T, Error> {
        do {
            let v: T = try await client.send(path, hapticOnError: false)
            return .success(v)
        } catch {
            return .failure(error)
        }
    }

    /// A good read replaces what is on screen; a 403 (no Food Cost access)
    /// or a cancelled load shows nothing new; any other failure keeps the
    /// last good read, or says the server's sentence when there is none.
    private static func adopt<T>(_ r: Result<T, Error>, keeping old: T?) -> (T?, String?) {
        switch r {
        case .success(let v):
            return (v, nil)
        case .failure(let error):
            if error is CancellationError { return (old, nil) }
            if let api = error as? APIClient.APIError {
                if api.status == 403 || api.status == 404 { return (nil, nil) }
                return old != nil ? (old, nil) : (nil, api.message)
            }
            return old != nil ? (old, nil) : (nil, "Couldn\u{2019}t read this just now.")
        }
    }
}
