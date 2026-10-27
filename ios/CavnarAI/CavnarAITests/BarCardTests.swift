import XCTest
@testable import CavnarAI

/// The Bar card (UIX-10 / F6), Count the bar, and the alcohol targets'
/// parsing (V47 / UIX-18). Fixtures/food_cost_bar_scenario.json is a recording of
/// bar_cost.build for Simple EJ's, trimmed to the keys the app decodes —
/// every value as the server sent it.
final class BarCardTests: XCTestCase {

    private func fixture() throws -> BarPayload {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "food_cost_bar_scenario", withExtension: "json"))
        return try JSONDecoder.cavnar.decode(BarPayload.self, from: Data(contentsOf: url))
    }

    private func decode(_ json: String) throws -> BarPayload {
        try JSONDecoder.cavnar.decode(BarPayload.self, from: Data(json.utf8))
    }

    // MARK: - (a) The recorded payload

    func testTheRecordedPayloadDecodes() throws {
        let p = try fixture()
        XCTAssertTrue(p.ok)
        XCTAssertTrue(p.available)
        XCTAssertEqual(p.asOfMdy, "10/9/26")

        let pc = try XCTUnwrap(p.pourCost)
        XCTAssertEqual(pc.total?.pct, 21.8, "the four-week figure")
        XCTAssertEqual(pc.basis, "purchases")
        XCTAssertEqual(pc.label, "Bought vs sold", "the chip reads the payload's own name for the figure")
        XCTAssertEqual(pc.window?.fromMdy, "9/12/26")
        XCTAssertEqual(pc.window?.toMdy, "10/9/26")
        XCTAssertEqual(pc.window?.days, 28)
        XCTAssertTrue(p.isJudged)
        XCTAssertNil(p.notJudgedText)
        XCTAssertEqual(FoodCostBarCard.windowLine(pc),
                       "What you bought from distributors as a % of what the POS sold \u{00B7} 9/12/26 \u{2013} 10/9/26")

        let top = try XCTUnwrap(p.topInsight)
        XCTAssertEqual(top.key, "over:liquor")
        XCTAssertEqual(top.tone, "info")
        XCTAssertEqual(top.headline, "Liquor bought vs sold is above the range for counted pour cost")
        XCTAssertTrue(top.metric?.hasPrefix("21.0% against 14\u{2013}20%") == true)
        XCTAssertNotNil(top.action)
        XCTAssertEqual(top.confidenceLine, "Evidence 100% \u{00B7} Freshness 75%")
        XCTAssertEqual(top.kicker, "Good to know")
        XCTAssertFalse(p.wantsHome, "an info insight keeps the card off Home")

        let rises = p.headlineIncreases
        XCTAssertEqual(rises.count, 1)
        XCTAssertEqual(rises.first?.name, "Titos VDK 1L")
        XCTAssertEqual(rises.first?.distributor, "Breakthru Beverage")
        XCTAssertEqual(rises.first?.forwardMonthlyUsd, 521.83)
        XCTAssertEqual(rises.first?.impactLine, "+$522 a month")
        XCTAssertEqual(rises.first?.sinceMdy, "9/10/26")

        XCTAssertEqual(p.feed?.invoicesThroughMdy, "10/9/26")
        XCTAssertEqual(p.feed?.salesThroughMdy, "10/14/26")
        XCTAssertEqual(p.feed?.lastFileMdy, "10/15/26")
        XCTAssertNil(p.feed?.staleDays)
        XCTAssertEqual(p.feedLines, [], "a current feed and no gaps: no amber line")

        XCTAssertEqual(p.counted?.available, false)
        XCTAssertEqual(p.counted?.nextDueMdy, "10/31/26")
        XCTAssertEqual(p.counted?.line, "Next count due 10/31/26")
    }

    func testHomeShowsTheCardOnlyForAWarningOrAProblem() throws {
        for (tone, shows) in [("bad", true), ("warn", true), ("info", false), ("good", false)] {
            let p = try decode("""
            {"ok": true, "available": true, "insights": [{"key": "k", "tone": "\(tone)", "headline": "H"}]}
            """)
            XCTAssertEqual(p.wantsHome, shows, tone)
        }
        let unavailable = try decode(#"{"ok": true, "available": false, "insights": [{"tone": "bad", "headline": "H"}]}"#)
        XCTAssertFalse(unavailable.wantsHome)
    }

    func testANotJudgedFigureSaysSoInsteadOfATone() throws {
        let withText = try decode("""
        {"ok": true, "available": true,
         "pour_cost": {"basis": "purchases", "label": "Bought vs sold", "judged": false,
                       "total": {"pct": 24.0, "judged": false, "tone": "bad",
                                 "not_judged_text": "Not judged \u{2014} Southern's invoices are missing."}}}
        """)
        XCTAssertFalse(withText.isJudged)
        XCTAssertEqual(withText.notJudgedText, "Not judged \u{2014} Southern's invoices are missing.")

        let totalOnly = try decode("""
        {"ok": true, "available": true, "pour_cost": {"judged": true, "total": {"pct": 24.0, "judged": false}}}
        """)
        XCTAssertFalse(totalOnly.isJudged, "either flag false is not judged")
        XCTAssertEqual(totalOnly.notJudgedText, BarPayload.notJudgedFallback)
        XCTAssertEqual(BarPayload.notJudgedFallback,
                       "Not judged yet \u{2014} the figures are what\u{2019}s on file so far.")
    }

    func testMissingInvoicesAndAQuietFeedReadInAmber() throws {
        let p = try decode("""
        {"ok": true, "available": true,
         "pour_cost": {"gaps": [{"distributor": "Southern Glazer's", "from_mdy": "9/20/26", "to_mdy": "10/3/26",
                                 "categories": ["liquor"]}],
                       "freshness": {"stale": true, "invoices_through_mdy": "10/2/26"}},
         "feed": {"invoices_through_mdy": "10/2/26", "stale_days": 13}}
        """)
        XCTAssertEqual(p.feedLines, [
            "Invoices from Southern Glazer's aren\u{2019}t on file for 9/20/26\u{2013}10/3/26",
            "No invoices since 10/2/26",
        ])
        let feedOnly = try decode(#"{"ok": true, "available": true, "feed": {"invoices_through_mdy": "10/2/26", "stale_days": 9}}"#)
        XCTAssertEqual(feedOnly.feedLines, ["No invoices since 10/2/26"])
    }

    func testTheCardDecodesWhateverTheBackendIsStillGrowing() throws {
        let unavailable = try decode("""
        {"ok": true, "available": false,
         "why_not": "No distributor invoices yet. Once Fintech's daily file reaches Cavnar AI, the bar's prices fill in here."}
        """)
        XCTAssertFalse(unavailable.available)
        XCTAssertTrue(unavailable.whyNot?.hasPrefix("No distributor invoices yet.") == true)
        XCTAssertNil(unavailable.pourCost)

        // Odd types empty one line, never the card.
        let odd = try decode("""
        {"ok": 1, "available": 1, "pour_cost": {"total": {"pct": "19.5"}, "gaps": "none", "label": null},
         "insights": [{"headline": "A"}, 7, {"tone": "bad"}],
         "price_watch": {"increases": [{"name": "Modelo", "headline": 1, "pct": 4, "forward_monthly_usd": null},
                                       {"name": "Rumple", "headline": false}, "junk"]},
         "counted": [], "feed": null}
        """)
        XCTAssertTrue(odd.ok)
        XCTAssertTrue(odd.available)
        XCTAssertEqual(odd.pourCost?.total?.pct, 19.5)
        XCTAssertNil(odd.pourCost?.label)
        XCTAssertEqual(odd.pourCost?.gaps.count, 0)
        XCTAssertEqual(odd.insights.map(\.headline), ["A"], "an insight without a headline is dropped")
        XCTAssertEqual(odd.headlineIncreases.map(\.name), ["Modelo"])
        XCTAssertEqual(odd.headlineIncreases.first?.impactLine, "+4%", "no money figure: the percentage")
        XCTAssertNil(odd.counted)
    }

    func testTheCardShowsAtMostThreeHeadlinePriceRises() throws {
        let rows = (1...5).map { #"{"product_key": "k\#($0)", "name": "P\#($0)", "headline": true, "forward_monthly_usd": \#($0 * 10)}"# }
        let p = try decode(#"{"ok": true, "available": true, "price_watch": {"increases": ["# + rows.joined(separator: ",") + "]}}")
        XCTAssertEqual(p.headlineIncreases.map(\.name), ["P1", "P2", "P3"], "the server's order, top three")
    }

    // MARK: - (b) The figure's name comes from the payload

    /// A purchases figure is "Bought vs sold", not a counted one: the card
    /// must never name the figure itself. Every string literal in the card
    /// and its sheet is checked; the chip reads `pc.label`.
    func testTheBarCardNeverHardcodesTheFiguresName() throws {
        for file in ["Features/FoodCost/FoodCostBarCard.swift", "Features/FoodCost/FoodCostBarCountSheet.swift"] {
            let src = try EdgeSource.read(file)
            let literals = Self.stringLiterals(src)
            XCTAssertFalse(literals.isEmpty, file)
            for lit in literals {
                XCTAssertNil(lit.range(of: "pour cost", options: .caseInsensitive),
                             "\(file) names the figure in a literal: \(lit)")
            }
        }
        let card = try EdgeSource.read("Features/FoodCost/FoodCostBarCard.swift")
        XCTAssertTrue(card.contains("if let label = pc.label"), "the basis chip is the payload's label")
        XCTAssertTrue(card.contains("TonePill(text: label, tone: .neutral)"))
        XCTAssertFalse(card.contains("Chart"), "no charts on the phone's Bar card")
    }

    /// The contents of every "…" literal, escapes left as written.
    static func stringLiterals(_ src: String) -> [String] {
        let re = try! NSRegularExpression(pattern: #""((?:[^"\\\n]|\\.)*)""#)
        let ns = src as NSString
        return re.matches(in: src, range: NSRange(location: 0, length: ns.length)).map {
            ns.substring(with: $0.range(at: 1))
        }
    }

    // MARK: - Count the bar

    func testACountSendsOnlyTheCategoriesTyped() throws {
        let us = Locale(identifier: "en_US")
        let body = try BarCountBody.make(date: "2026-10-15", beer: "1,240.50", liquor: "", wine: " 860 ",
                                         note: "  ", locale: us).get()
        XCTAssertEqual(body.countDate, "2026-10-15")
        XCTAssertEqual(body.values, ["beer": 1240.5, "wine": 860], "a blank is left out, never sent as 0")
        XCTAssertNil(body.note)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder.cavnar.encode(body)) as? [String: Any])
        XCTAssertEqual(Set(json.keys), ["count_date", "values"])
        XCTAssertEqual(json["values"] as? [String: Double], ["beer": 1240.5, "wine": 860])

        let withNote = try BarCountBody.make(date: "2026-10-15", beer: "", liquor: "2.310,75", wine: "",
                                             note: "Walk-in keg room too", locale: Locale(identifier: "de_DE")).get()
        XCTAssertEqual(withNote.values, ["liquor": 2310.75], "read for the phone's locale")
        let json2 = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder.cavnar.encode(withNote)) as? [String: Any])
        XCTAssertEqual(json2["note"] as? String, "Walk-in keg room too")
    }

    func testACountThatIsntANumberOrIsEmptySaysSo() {
        let us = Locale(identifier: "en_US")
        XCTAssertEqual(BarCountBody.make(date: "2026-10-15", beer: "lots", liquor: "", wine: "", note: "", locale: us),
                       .failure(BarCountError(field: "beer", message: "Enter an amount in dollars.")))
        XCTAssertEqual(BarCountBody.make(date: "2026-10-15", beer: "", liquor: "", wine: "", note: "", locale: us),
                       .failure(BarCountError(field: nil, message: "Type a value for beer, liquor or wine.")))
        XCTAssertEqual(BarCountBody.entry("-5", locale: us), .invalid)
        XCTAssertEqual(BarCountBody.entry("$412", locale: us), .value(412))
    }

    func testTheCountAnswerDecodes() throws {
        let ok = try JSONDecoder.cavnar.decode(BarCountResponse.self, from: Data("""
        {"ok": true, "count_date": "2026-10-15", "count_date_mdy": "10/15/26", "saved": ["beer", "wine"]}
        """.utf8))
        XCTAssertTrue(ok.ok)
        XCTAssertEqual(ok.countDateMdy, "10/15/26")
        XCTAssertEqual(ok.saved, ["beer", "wine"])
        let refused = try JSONDecoder.cavnar.decode(BarCountResponse.self, from: Data(
            #"{"ok": false, "error": "A count can't be dated in the future."}"#.utf8))
        XCTAssertFalse(refused.ok)
        XCTAssertEqual(refused.error, "A count can't be dated in the future.")
    }

    // MARK: - (c) The alcohol targets' parsing (V47 / UIX-18)

    private func targets() throws -> TargetsPayload {
        try JSONDecoder.cavnar.decode(TargetsPayload.self, from: Data("""
        {"labor_target_pct": 30, "food_cost_target": 28, "bar_cost_target": 20, "beer_cost_target": null,
         "liquor_cost_target": 18.5, "wine_cost_target": null}
        """.utf8))
    }

    func testATargetThatIsntANumberIsAnErrorNotASilentNoSave() throws {
        let us = Locale(identifier: "en_US")
        XCTAssertEqual(TargetsBody.parse("abc", locale: us), .invalid)
        XCTAssertEqual(TargetsBody.Draft.invalidMessage, "Enter a number")
        XCTAssertEqual(TargetsBody.commit(TargetsPayload.Field.bar, typed: "abc", current: try targets(), locale: us),
                       .error("Enter a number"))
        XCTAssertEqual(TargetsBody.commit(TargetsPayload.Field.wine, typed: "18.5.3", current: try targets(), locale: us),
                       .error("Enter a number"))
    }

    func testACommaDecimalPhoneReadsEighteenAndAHalf() throws {
        for id in ["de_DE", "fr_FR"] {
            let loc = Locale(identifier: id)
            XCTAssertEqual(TargetsBody.parse("18,5", locale: loc), .value(18.5), id)
            XCTAssertEqual(TargetsBody.commit(TargetsPayload.Field.beer, typed: "18,5", current: try targets(), locale: loc),
                           .save(TargetsBody(number: [TargetsPayload.Field.beer: 18.5])), "never 185 (\(id))")
            // The stored 18.5 reads back in the phone's own form, so an
            // untouched box sends nothing.
            XCTAssertEqual(try targets().text(for: TargetsPayload.Field.liquor, locale: loc), "18,50", id)
            XCTAssertEqual(TargetsBody.commit(TargetsPayload.Field.liquor, typed: "18,50", current: try targets(), locale: loc),
                           .nothing, id)
        }
        let us = Locale(identifier: "en_US")
        XCTAssertEqual(TargetsBody.parse("18.5", locale: us), .value(18.5))
        XCTAssertEqual(TargetsBody.parse("18.5%", locale: us), .value(18.5))
        XCTAssertEqual(TargetsBody.commit(TargetsPayload.Field.wine, typed: "18.5", current: try targets(), locale: us),
                       .save(TargetsBody(number: [TargetsPayload.Field.wine: 18.5])))
        // A point decimal from a hardware keyboard on a comma phone.
        XCTAssertEqual(TargetsBody.parse("18.5", locale: Locale(identifier: "de_DE")), .value(18.5))
    }

    func testABlankTargetIsNotSet() throws {
        let us = Locale(identifier: "en_US")
        XCTAssertEqual(TargetsBody.parse("", locale: us), .blank)
        XCTAssertEqual(TargetsBody.parse("   ", locale: us), .blank)
        XCTAssertEqual(TargetsBody.commit(TargetsPayload.Field.bar, typed: "", current: try targets(), locale: us),
                       .save(TargetsBody(clearKeys: [TargetsPayload.Field.bar])), "back to each category's range")
        XCTAssertEqual(TargetsBody.commit(TargetsPayload.Field.beer, typed: "", current: try targets(), locale: us),
                       .nothing, "already not set: nothing to send")
    }

    func testTheAlcoholRowsSayWhatABlankUses() {
        XCTAssertEqual(TargetsPayload.alcoholCaption(TargetsPayload.Field.bar),
                       "Beer, liquor and wine bought as a % of their sales, on the Bar page. Blank uses each category\u{2019}s range.")
        XCTAssertTrue(TargetsPayload.alcoholCaption(TargetsPayload.Field.beer)?.contains("20\u{2013}26%, Cavnar AI\u{2019}s starting range") == true)
        XCTAssertTrue(TargetsPayload.alcoholCaption(TargetsPayload.Field.liquor)?.contains("14\u{2013}20%") == true)
        XCTAssertTrue(TargetsPayload.alcoholCaption(TargetsPayload.Field.wine)?.contains("30\u{2013}40%") == true)
        XCTAssertNil(TargetsPayload.alcoholCaption(TargetsPayload.Field.labor))
    }
}
