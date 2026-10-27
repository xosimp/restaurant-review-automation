import SwiftUI
import XCTest
@testable import CavnarAI

/// The six main screens' glance — what each opens on — rendered from the
/// CAPTURED fixtures (ResponseShapeContractTests' files) at the largest
/// accessibility text size, and measured: a size check, not a pixel diff
/// (release-safety audit #25, A06-14).
///
/// Each screen is built from the components it really draws (Home's brief
/// card and KPI tiles, the inbox's review rows, the Labor and Food Cost
/// answer cards and tiles, the night's status pill, net-sales tile and first
/// action, the staff app's day rows), fed the real payload through the
/// screens' own static readers. Every key piece must, at `.accessibility3`:
///
/// * have a size — never collapse to zero (an empty string, a hidden frame);
/// * fit the phone's width — nothing pushed past the edge and cut off;
/// * be TALLER than at the default size — a piece that does not grow with
///   the text inside it is a fixed frame clipping that text.
@MainActor
final class DynamicTypeRenderTests: XCTestCase {
    /// A 6.1" iPhone's width less the app's 16pt gutters.
    static let width: CGFloat = 390 - 32

    /// Each piece's laid-out size, written by its probe while the renderer
    /// lays the glance out.
    final class Frames {
        var sizes: [String: CGSize] = [:]
    }

    private struct Probe: ViewModifier {
        let name: String
        let frames: Frames

        func body(content: Content) -> some View {
            content.background(
                GeometryReader { geo in
                    let _ = frames.sizes.updateValue(geo.size, forKey: name)
                    Color.clear
                }
            )
        }
    }

    // MARK: - Fixtures

    private static func payload(_ name: String, part: String? = nil) throws -> Data {
        let url = try XCTUnwrap(Bundle(for: DynamicTypeRenderTests.self).url(forResource: name, withExtension: "json"))
        let data = try Data(contentsOf: url)
        guard let part else { return data }
        let all = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        return try JSONSerialization.data(withJSONObject: try XCTUnwrap(all[part]))
    }

    private static func decode<T: Decodable>(_ type: T.Type, _ name: String, part: String? = nil) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: payload(name, part: part))
    }

    // MARK: - Rendering and measuring

    /// The glance at `size`: the image and every probe's size.
    private func render<V: View>(_ size: DynamicTypeSize, _ glance: (Frames) -> V) -> (UIImage?, [String: CGSize]) {
        let frames = Frames()
        let renderer = ImageRenderer(content:
            glance(frames)
                .frame(width: Self.width, alignment: .topLeading)
                .environment(\.dynamicTypeSize, size)
                .preferredColorScheme(.dark))
        renderer.proposedSize = ProposedViewSize(width: Self.width, height: nil)
        renderer.scale = 1
        let image = renderer.uiImage
        return (image, frames.sizes)
    }

    /// The three rules above, for every probe of one screen.
    private func assertReadable<V: View>(_ screen: String, file: StaticString = #filePath, line: UInt = #line,
                                         _ glance: @escaping (Frames) -> V) {
        let (image, big) = render(.accessibility3, glance)
        let (_, regular) = render(.large, glance)
        XCTAssertNotNil(image, "\(screen): the glance did not render", file: file, line: line)
        XCTAssertGreaterThan(image?.size.height ?? 0, 0, "\(screen): the glance has no height", file: file, line: line)
        XCTAssertFalse(big.isEmpty, "\(screen): no piece was laid out", file: file, line: line)
        for (name, size) in big.sorted(by: { $0.key < $1.key }) {
            XCTAssertGreaterThan(size.width, 1, "\(name) collapsed to no width", file: file, line: line)
            XCTAssertGreaterThan(size.height, 1, "\(name) collapsed to no height", file: file, line: line)
            XCTAssertLessThanOrEqual(size.width, Self.width + 0.5,
                                     "\(name) is wider than the phone (\(size.width) > \(Self.width)): cut off",
                                     file: file, line: line)
            if let before = regular[name] {
                XCTAssertGreaterThan(size.height, before.height + 0.5,
                                     "\(name) did not grow with the text (\(before.height) → \(size.height)): a fixed "
                                     + "frame is clipping it", file: file, line: line)
            } else {
                XCTFail("\(name) was laid out only at the largest size", file: file, line: line)
            }
        }
    }

    private func probe<V: View>(_ view: V, _ name: String, _ frames: Frames) -> some View {
        view.modifier(Probe(name: name, frames: frames))
    }

    // MARK: - The six screens

    func testHome() throws {
        let home = try Self.decode(HomeSummary.self, "home")
        let modules = Array(home.modules.filter(\.isAvailable).prefix(4))
        XCTAssertFalse(modules.isEmpty, "the fixture's Home has modules")
        let headline = HomeView.briefHeadline(home) ?? home.restaurantName
        assertReadable("Home") { f in
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                self.probe(CavnarAnswerCard(kicker: "Today", headline: headline), "home.brief", f)
                ForEach(modules, id: \.key) { module in
                    self.probe(KPITile(module: module), "home.tile.\(module.key)", f)
                }
            }
        }
    }

    func testReviews() throws {
        struct Page: Decodable { let reviews: [Review] }
        let rows = Array(try Self.decode(Page.self, "reviews").reviews.prefix(3))
        XCTAssertEqual(rows.count, 3, "the fixture's inbox has reviews")
        assertReadable("Reviews") { f in
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                ForEach(rows, id: \.id) { review in
                    self.probe(ReviewRow(review: review), "reviews.row.\(review.id)", f)
                }
            }
        }
    }

    func testLabor() throws {
        let stats = try Self.decode(LaborStats.self, "labor")
        let answer = LaborAnalyticsSection.answerLine(stats)
        XCTAssertFalse(answer.isEmpty)
        let pct = String(format: "%.1f%%", stats.overallLaborPct)
        assertReadable("Labor") { f in
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                self.probe(CavnarAnswerCard(kicker: "Labor", headline: answer), "labor.answer", f)
                self.probe(DSRStatTile(label: "Labor cost", value: pct,
                                       detail: LaborAnalyticsSection.targetName(stats)), "labor.tile", f)
            }
        }
    }

    func testFoodCost() throws {
        let food = try Self.decode(FoodCostAnalytics.self, "food_cost")
        let headline = FoodCostAnalyticsSection.projectionLine(food) ?? food.insightIntro ?? "Food cost this week"
        let trust = FoodCostAnalyticsSection.trustLines(food).first
        assertReadable("Food Cost") { f in
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                self.probe(CavnarAnswerCard(kicker: "Food cost", headline: headline, summary: trust), "food.answer", f)
                if let first = food.insightRecommendations.first {
                    self.probe(CavnarAnswerCard(kicker: "Do next", headline: first), "food.recommendation", f)
                }
            }
        }
    }

    func testTheNightsReport() throws {
        let report = try Self.decode(DSRReport.self, "dsr_preview", part: "owner")
        let net = DSRFormat.money(report.facts.blocks["sales"]?.metric("net"))
        let action = report.narrative?.actionsTomorrow.first
        assertReadable("Daily Report") { f in
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                self.probe(DSRStatusPill(phase: report.phase), "dsr.status", f)
                self.probe(DSRStatTile(label: "Net sales", value: net), "dsr.net", f)
                if let lead = report.narrative?.executiveSummary?.text {
                    self.probe(CavnarAnswerCard(kicker: "The night", headline: lead), "dsr.lead", f)
                }
                if let action {
                    self.probe(CavnarAnswerCard(kicker: DSRDoThisToday.kicker(action), headline: action.text,
                                                summary: action.whyFirstSentence), "dsr.action", f)
                }
            }
        }
    }

    func testStaffToday() throws {
        let shifts = try Self.decode(StaffShiftsResponse.self, "staff_shifts")
        let days = Array((shifts.week ?? []).prefix(3))
        XCTAssertFalse(days.isEmpty, "the fixture's week has days")
        assertReadable("Staff Today") { f in
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                ForEach(days, id: \.date) { day in
                    self.probe(StaffDayRow(day: day, onChange: { _, _ in }), "staff.day.\(day.date)", f)
                }
            }
        }
    }
}
