import XCTest
@testable import CavnarAI

/// Never-say audit, workstream C: the owner-copy rules that are logic
/// (OwnerCopy), and the payload fields they read, decoded tolerantly.
final class OwnerCopyTests: XCTestCase {

    // MARK: money kinds

    func testKindWordNamesEachKindAndOnlyMeasuredReadsAsMeasured() {
        XCTAssertEqual(OwnerCopy.kindWord("measured"), "measured")
        XCTAssertEqual(OwnerCopy.kindWord("opportunity"), "at stake \u{00B7} not captured")
        XCTAssertEqual(OwnerCopy.kindWord("projection"), "projected")
        XCTAssertEqual(OwnerCopy.kindWord("estimate"), "estimated")
        XCTAssertEqual(OwnerCopy.kindWord("PLAN"), "budget")
        XCTAssertNil(OwnerCopy.kindWord(nil))
        XCTAssertNil(OwnerCopy.kindWord("something new"))
        for k in ["computed", "estimate", "opportunity", "projection", "forecast", "plan", "benchmark", "price"] {
            let w = OwnerCopy.kindWord(k) ?? ""
            XCTAssertFalse(w.contains("measured"), k)
            XCTAssertFalse(w.lowercased().contains("sav"), k)
        }
    }

    func testARecommendationChipReadsItsKindAndDefaultsToAtStake() throws {
        let json = #"{"key":"k","title":"Trim Tuesday","dollars_monthly":420,"dollars_kind":"projection"}"#
        let rec = try JSONDecoder.cavnar.decode(HomeRecommendation.self, from: Data(json.utf8))
        XCTAssertEqual(rec.dollarsKind, "projection")
        XCTAssertEqual(HomeRecommendations.chips(rec).first, "$420/mo projected")
        let old = try JSONDecoder.cavnar.decode(HomeRecommendation.self,
                                                from: Data(#"{"key":"k","title":"t","dollars_monthly":420}"#.utf8))
        XCTAssertEqual(HomeRecommendations.chips(old).first, "$420/mo at stake")
    }

    // MARK: positive status

    func testLaborAtZeroWithNoSalesIsNeverExcellentOrOnTarget() {
        let allowed = OwnerCopy.laborPositiveAllowed(isLive: true, dataComplete: nil, salesDataMissing: true,
                                                     hoursAreEstimated: false, periodTooShort: false, pct: 0)
        XCTAssertFalse(allowed)
        let s = OwnerCopy.laborBucket(pct: 0, target: 30, isLive: true, positiveAllowed: allowed)
        XCTAssertEqual(s.tone, .neutral)
        XCTAssertFalse(s.label.contains("Excellent"))
        XCTAssertFalse(s.label.contains("On Target"))
    }

    func testLaborBucketOnlySaysOnTargetWhenLiveAndComplete() {
        XCTAssertEqual(OwnerCopy.laborBucket(pct: 29, target: 30, isLive: true, positiveAllowed: true),
                       .init(label: "On Target", tone: .good))
        XCTAssertEqual(OwnerCopy.laborBucket(pct: 25, target: 30, isLive: true, positiveAllowed: true).tone, .good)
        XCTAssertEqual(OwnerCopy.laborBucket(pct: 29, target: 30, isLive: false, positiveAllowed: true),
                       .init(label: "Sample", tone: .neutral))
        XCTAssertEqual(OwnerCopy.laborBucket(pct: 36.8, target: 30, isLive: false, positiveAllowed: false),
                       .init(label: "Sample", tone: .neutral))
        XCTAssertEqual(OwnerCopy.laborBucket(pct: 29, target: 30, isLive: true, positiveAllowed: false).tone, .neutral)
        // Over target always says so, partial data or not.
        XCTAssertEqual(OwnerCopy.laborBucket(pct: 40, target: 30, isLive: true, positiveAllowed: false).tone, .bad)
        XCTAssertEqual(OwnerCopy.laborBucket(pct: 31, target: 30, isLive: true, positiveAllowed: false).tone, .warn)
        for pct in stride(from: 0.0, through: 50.0, by: 0.5) {
            XCTAssertNotEqual(OwnerCopy.laborBucket(pct: pct, target: 30, isLive: true, positiveAllowed: true).label, "Excellent")
        }
        XCTAssertFalse(OwnerCopy.laborPositiveAllowed(isLive: true, dataComplete: false, salesDataMissing: false,
                                                      hoursAreEstimated: false, periodTooShort: false, pct: 28))
        XCTAssertFalse(OwnerCopy.laborPositiveAllowed(isLive: true, dataComplete: true, salesDataMissing: false,
                                                      hoursAreEstimated: true, periodTooShort: false, pct: 28))
        XCTAssertFalse(OwnerCopy.laborPositiveAllowed(isLive: true, dataComplete: true, salesDataMissing: false,
                                                      hoursAreEstimated: false, periodTooShort: true, pct: 28))
        XCTAssertTrue(OwnerCopy.laborPositiveAllowed(isLive: true, dataComplete: true, salesDataMissing: false,
                                                     hoursAreEstimated: false, periodTooShort: false, pct: 28))
    }

    func testAllClearNeedsTheServersAllClearALiveSourceAndNoneStale() throws {
        let clear = try JSONDecoder.cavnar.decode(HomeMonitoring.self,
            from: Data(#"{"count_live":3,"stale":0,"all_clear":true,"stalest_as_of":"2026-09-23"}"#.utf8))
        XCTAssertEqual(clear.allClear, true)
        XCTAssertEqual(clear.stale, 0)
        XCTAssertTrue(OwnerCopy.allClear(attentionEmpty: true, monitoring: clear).clear)

        let stale = try JSONDecoder.cavnar.decode(HomeMonitoring.self,
            from: Data(#"{"count_live":2,"stale":1,"all_clear":false}"#.utf8))
        let r = OwnerCopy.allClear(attentionEmpty: true, monitoring: stale)
        XCTAssertFalse(r.clear)
        XCTAssertTrue(r.reason?.contains("out of date") == true)

        let dayOne = HomeMonitoring(countLive: 0, stalestAsOf: nil)
        XCTAssertFalse(OwnerCopy.allClear(attentionEmpty: true, monitoring: dayOne).clear)
        XCTAssertFalse(OwnerCopy.allClear(attentionEmpty: true, monitoring: nil).clear)
        XCTAssertFalse(OwnerCopy.allClear(attentionEmpty: true,
                                          monitoring: HomeMonitoring(countLive: 2, stalestAsOf: nil, stale: 0, allClear: false)).clear)
        // An older server's monitoring object (no stale / all_clear) still decodes.
        let old = try JSONDecoder.cavnar.decode(HomeMonitoring.self, from: Data(#"{"count_live":2}"#.utf8))
        XCTAssertNil(old.allClear)
        XCTAssertTrue(OwnerCopy.allClear(attentionEmpty: true, monitoring: old).clear)
    }

    func testTheHeroOnlyClaimsAIAtWorkOverALiveSource() {
        XCTAssertTrue(HomeView.heroTail(liveSources: 2).contains("running on AI"))
        XCTAssertFalse(HomeView.heroTail(liveSources: 0).contains("running on AI"))
        XCTAssertFalse(HomeView.heroTail(liveSources: nil).contains("running on AI"))
    }

    // MARK: labor money

    func testLaborMoneyIsAGapNeverSavingsAndNeverGreen() {
        let tiles = OwnerCopy.laborMoneyTiles(isLive: true, monthly: 1765, annual: 21180, vsIndustryMonthly: 0,
                                              vsIndustryAnnual: 0, industryText: "34.5%", periodDays: 14)
        XCTAssertEqual(tiles.map(\.label), ["Gap to target / mo", "Per year \u{00B7} projected"])
        XCTAssertEqual(tiles.first?.sublabel, "available, not captured")
        XCTAssertEqual(tiles.last?.sublabel, "from a 14-day window")
        for t in tiles {
            XCTAssertFalse((t.label + t.sublabel).lowercased().contains("saving"), t.label)
            XCTAssertNotEqual(t.tone, .good)
        }
        let industry = OwnerCopy.laborMoneyTiles(isLive: true, monthly: 0, annual: 0, vsIndustryMonthly: 900,
                                                 vsIndustryAnnual: 10800, industryText: "34.5%", periodDays: 14)
        XCTAssertEqual(industry.first?.label, "Under 34.5% industry / mo")
        XCTAssertTrue(industry.allSatisfy { !$0.label.lowercased().contains("advantage") && $0.tone != .good })
    }

    func testSampleDataCarriesNoDollarsAndAZeroBenchmarkGapIsHidden() {
        XCTAssertTrue(OwnerCopy.laborMoneyTiles(isLive: false, monthly: 12770, annual: 153240, vsIndustryMonthly: 0,
                                                vsIndustryAnnual: 0, industryText: "34.5%", periodDays: 7).isEmpty)
        XCTAssertTrue(OwnerCopy.laborMoneyTiles(isLive: true, monthly: 0, annual: 0, vsIndustryMonthly: 0,
                                                vsIndustryAnnual: 0, industryText: "34.5%", periodDays: 7).isEmpty)
    }

    // MARK: diagnoses, DSR, schedule, labels

    func testExpectedOutcomeIsConditionalAndACertaintyIsWithheld() {
        XCTAssertEqual(OwnerCopy.expectedOutcomeLabel, "If this is the cause, you\u{2019}d expect\u{2026}")
        XCTAssertEqual(OwnerCopy.diagnosisHeading, "What the evidence points to")
        XCTAssertEqual(OwnerCopy.expectedOutcome("Slow-service mentions should ease within two weeks."),
                       "Slow-service mentions should ease within two weeks.")
        XCTAssertNil(OwnerCopy.expectedOutcome("This will eliminate the slow-service reviews within 2 weeks."))
        XCTAssertNil(OwnerCopy.expectedOutcome("Guaranteed to fix it."))
        XCTAssertNil(OwnerCopy.expectedOutcome("   "))
    }

    func testTheDSRAtStakeLineCarriesItsBasisAndSaysPartial() {
        let line = OwnerCopy.dsrAtStakeLine(amount: "$782", basis: "4 cost drivers, one ingredient counted once.",
                                            complete: false) ?? ""
        XCTAssertTrue(line.hasPrefix("At stake each month: $782"))
        XCTAssertTrue(line.contains("an opportunity, not captured"))
        XCTAssertTrue(line.contains("(4 cost drivers, one ingredient counted once)"))
        XCTAssertTrue(line.contains("partial"))
        XCTAssertFalse(line.lowercased().contains("recoverable"))
        XCTAssertNil(OwnerCopy.dsrAtStakeLine(amount: nil, basis: nil, complete: nil))
    }

    func testTheDSRReadsTheRenamedSlotFirstAndLabelsItAnOpportunity() throws {
        let json = #"""
        {"largest_dollar_gap": {"text": "Beef is $540 a month above last quarter.", "cites": ["food.x"]},
         "largest_money_saving": {"text": "Old slot.", "cites": []},
         "biggest_financial_opportunity": {"text": "Waste above tolerance.", "cites": []}}
        """#
        let n = try JSONDecoder.cavnar.decode(DSRNarrative.self, from: Data(json.utf8))
        XCTAssertEqual(n.largestMoneyOpportunity?.text, "Beef is $540 a month above last quarter.")
        let labels = n.callouts.map(\.label)
        XCTAssertEqual(labels, ["Biggest opportunity \u{00B7} not captured",
                                "Largest dollar gap \u{00B7} an opportunity, not savings"])
        let old = try JSONDecoder.cavnar.decode(DSRNarrative.self,
                                                from: Data(#"{"largest_money_saving": {"text": "Old slot."}}"#.utf8))
        XCTAssertEqual(old.largestMoneyOpportunity?.text, "Old slot.")
    }

    func testTheScheduleProgressOnlyNamesLastYearWhenItExists() {
        XCTAssertEqual(ScheduleProgressSteps.steps(lastYearAvailable: false).first?.text,
                       "Reading your shift and sales history")
        XCTAssertEqual(ScheduleProgressSteps.steps(lastYearAvailable: true).first?.text,
                       "Reading last year's same days")
    }

    func testServerRecoverableLabelsReadAsOpportunities() {
        XCTAssertEqual(OwnerCopy.displayLabel("recoverable"), "opportunity / mo")
        XCTAssertEqual(OwnerCopy.displayLabel("recoverable / mo"), "opportunity / mo")
        XCTAssertEqual(OwnerCopy.displayLabel("pieces this month"), "pieces this month")
        XCTAssertEqual(OwnerCopy.ifIgnoredLabel, "Risk if left alone: ")
    }

    // MARK: Ask

    func testAskShowsUnsupportedCausesAndNamesInline() {
        let e = AskEvidence(modules: ["labor"], confidence: nil, unverifiedFigures: ["$1,240"],
                            unsupportedCauses: ["the new fryer caused the slow tickets"],
                            unsupportedNames: ["Marco"])
        XCTAssertEqual(e.warnings.count, 3)
        XCTAssertTrue(e.causeWarning?.hasPrefix("Unsupported cause") == true)
        XCTAssertTrue(e.causeWarning?.contains("the new fryer caused the slow tickets") == true)
        XCTAssertTrue(e.nameWarning?.contains("Marco") == true)
        let quiet = AskEvidence(modules: [], confidence: nil, unverifiedFigures: [],
                                unsupportedCauses: ["x"], unsupportedNames: [])
        XCTAssertFalse(quiet.isEmpty)
        XCTAssertTrue(AskEvidence().warnings.isEmpty)
    }
}
