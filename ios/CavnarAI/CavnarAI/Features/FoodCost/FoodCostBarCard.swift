import SwiftUI
import Observation

/// The Bar card (UIX-10 / F6) — "Web explains. iPhone decides." The web's
/// Food Cost → Bar page explains the bar; the phone answers in one card:
///
///   1. the four-week figure, with the payload's own name for it beside it
///      as a chip ("Bought vs sold" when it rests on purchases) — or, when
///      the figure isn't judged, the sentence that says so instead of a tone;
///   2. the one top insight, as a `CavnarAnswerCard` (headline, metric,
///      action) with its measured confidence;
///   3. an amber line when invoices are missing or the feed has gone quiet;
///   4. the top three price rises the server marked as headlines;
///   5. Count the bar (the shelf count, done behind the bar after close);
///   6. the Bar page on the web.
///
/// No charts, no tables, no category editing — those stay on the web. The
/// figure's name is never written here: it is `pourCost.label`, from the
/// server, because a purchases figure is not a counted one.
struct FoodCostBarCard: View {
    let viewModel: BarCardViewModel
    /// Draws the section title above the card (Food Cost). Home passes
    /// false and puts its own section header over it.
    var showsTitle = true

    @State private var counting = false

    var body: some View {
        if let p = viewModel.payload {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if showsTitle {
                    Text("Bar").cavnarText(.headline)
                        .accessibilityAddTraits(.isHeader)
                }
                if p.available {
                    readyCard(p)
                } else {
                    // Nothing on file yet (no Fintech invoices): the
                    // server's one sentence, muted, and nothing else.
                    Text(p.whyNot ?? "No distributor invoices yet.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .cavnarCard()
                }
            }
            .sheet(isPresented: $counting) {
                FoodCostBarCountSheet { saved in
                    viewModel.posted = saved
                    Task { await viewModel.load() }
                }
            }
        } else if viewModel.isLoading {
            // The house's loading state while the first read is out
            // (UI-20, 10/16/26: on Food Cost and Home the card used to
            // vanish until the payload arrived).
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if showsTitle {
                    Text("Bar").cavnarText(.headline)
                        .accessibilityAddTraits(.isHeader)
                }
                CavnarSkeletonLines(widths: [0.4, 1.0, 0.86, 0.55])
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .cavnarCard()
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel("Loading the bar")
            }
        } else if let why = viewModel.errorText {
            // The server's own sentence when the read failed — never a card
            // that silently isn't there (UI-20).
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if showsTitle {
                    Text("Bar").cavnarText(.headline)
                        .accessibilityAddTraits(.isHeader)
                }
                Text(why)
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .cavnarCard()
            }
        }
    }

    // MARK: - The card

    private func readyCard(_ p: BarPayload) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.m) {
            figure(p)

            let feedLines = p.feedLines
            if !feedLines.isEmpty {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    ForEach(feedLines, id: \.self) { line in
                        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .font(.cavnar(.caption))
                                .foregroundStyle(Color.cavnarAmber)
                                .accessibilityHidden(true)
                            CavnarMixedText(line + ".", role: .secondary, color: .cavnarAmber)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            }

            if let insight = p.topInsight {
                AccountRowDivider()
                insightCard(insight)
            }

            let rises = p.headlineIncreases
            if !rises.isEmpty {
                AccountRowDivider()
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    CavnarKicker("Prices that went up", isHeader: false)
                    ForEach(rises) { row in
                        priceRow(row)
                    }
                }
            }

            AccountRowDivider()
            countEntry(p)

            if let posted = viewModel.posted {
                CavnarInlinePosted(label: posted) { viewModel.posted = nil }
            }

            // "See all · Open on the web" — the house's reading verb
            // (DESIGN_SYSTEM: the two web-row verbs).
            CavnarWebLinkRow(title: "See all", subtitle: "The Bar page: every category, every price change and the trend",
                             path: "bar", actionLabel: "Open on the web")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    /// The four-week figure and its name; the not-judged sentence in place
    /// of a tone when the reading isn't judged.
    @ViewBuilder
    private func figure(_ p: BarPayload) -> some View {
        let pc = p.pourCost
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if let pc, pc.available != false {
                HStack(alignment: .center, spacing: CavnarSpace.s) {
                    if let pct = pc.total?.pct {
                        Text("\(pct, specifier: "%.1f")%")
                            .cavnarText(.figureL, color: p.isJudged ? Self.toneColor(pc.total?.tone) : .cavnarInk)
                            .minimumScaleFactor(0.85)
                            .lineLimit(1)
                    } else {
                        // Never a zero for a figure that isn't known.
                        Text("\u{2014}").cavnarText(.figureL, color: .cavnarInk3)
                            .accessibilityLabel("Not measured")
                    }
                    if let label = pc.label {
                        TonePill(text: label, tone: .neutral)
                    }
                    Spacer(minLength: 0)
                }
                if let line = Self.windowLine(pc) {
                    CavnarMixedText(line, role: .caption)
                }
                if let notJudged = p.notJudgedText {
                    CavnarMixedText(notJudged, role: .secondary)
                } else if let target = pc.total?.targetLabel {
                    CavnarMixedText(target, role: .secondary)
                }
            } else {
                Text(pc?.whyNot ?? BarPayload.notJudgedFallback)
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }

    /// The top insight — measured, so a plain surface inside this card,
    /// with the web's toned kicker over it.
    private func insightCard(_ insight: BarInsight) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker(insight.kicker, tint: Self.toneColor(insight.tone, neutral: .cavnarEmber2), isHeader: false)
            CavnarAnswerCard(headline: insight.headline, summary: insight.metric, surface: nil) {
                if let action = insight.action {
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        Image(systemName: "arrow.right")
                            .font(.cavnar(.secondary))
                            .foregroundStyle(Color.cavnarEmber2)
                            .accessibilityHidden(true)
                        CavnarMixedText(action, role: .secondary)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
            if let confidence = insight.confidenceLine {
                CavnarMixedText(confidence, role: .caption)
            }
        }
    }

    private func priceRow(_ row: BarPriceIncrease) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
            VStack(alignment: .leading, spacing: 2) {
                Text(row.name).cavnarText(.label)
                    .fixedSize(horizontal: false, vertical: true)
                if let distributor = row.distributor {
                    Text(distributor).cavnarText(.caption)
                }
            }
            Spacer(minLength: CavnarSpace.xs)
            if let impact = row.impactLine {
                CavnarMixedText(impact, role: .secondary, color: .cavnarRedText)
                    .multilineTextAlignment(.trailing)
            }
        }
        .padding(.vertical, CavnarSpace.xxs)
        .accessibilityElement(children: .combine)
    }

    private func countEntry(_ p: BarPayload) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            Button {
                Haptic.light()
                counting = true
            } label: {
                Label("Count the bar", systemImage: "list.clipboard")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .accessibilityHint("Value beer, liquor and wine at what you paid")
            if let line = p.counted?.line {
                CavnarMixedText(line, role: .caption)
            }
        }
    }

    // MARK: - Helpers

    /// "What you bought … sold · 9/12/26 – 10/9/26".
    static func windowLine(_ pc: BarPourCost) -> String? {
        var parts: [String] = []
        if let sub = pc.sub { parts.append(sub.prefix(1).uppercased() + sub.dropFirst()) }
        if let a = pc.window?.fromMdy, let b = pc.window?.toMdy { parts.append("\(a) \u{2013} \(b)") }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }

    static func toneColor(_ tone: String?, neutral: Color = .cavnarInk) -> Color {
        switch tone {
        case "good": return .cavnarGreen
        case "warn": return .cavnarAmber
        case "bad": return .cavnarRedText
        default: return neutral
        }
    }
}

// MARK: - Model

/// Reads the Bar payload for the card. Best-effort like Food Cost's other
/// side reads: a refusal (403 — a login without the Bar) hides the card; a
/// first read still out shows the shared skeleton; a failed read with
/// nothing on screen shows the server's sentence (UI-20), never the screen.
@Observable
@MainActor
final class BarCardViewModel {
    private(set) var payload: BarPayload?
    private(set) var isLoading = false
    /// The server's sentence when the read failed and nothing is on screen.
    private(set) var errorText: String?
    /// "Bar count saved for 10/15/26" after a count, until it fades.
    var posted: String?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    static let failedSentence = "The bar's numbers couldn't be read just now. Try again in a minute."

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let p: BarPayload = try await client.send("/mobile/api/food-cost/bar", hapticOnError: false)
            // `ok: false` is the server failing to build it — the sentence,
            // unless an earlier good read is already on screen.
            if p.ok {
                payload = p
                errorText = nil
            } else if payload == nil {
                errorText = Self.failedSentence
            }
        } catch is CancellationError {
            // The screen went away mid-load.
        } catch let error as APIClient.APIError where error.status == 403 {
            payload = nil
            errorText = nil
        } catch let error as APIClient.APIError {
            // Keep what is on screen; with nothing on screen, say why.
            if payload == nil { errorText = error.message.isEmpty ? Self.failedSentence : error.message }
        } catch {
            if payload == nil { errorText = Self.failedSentence }
        }
    }
}
