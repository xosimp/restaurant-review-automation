import XCTest
import UIKit
@testable import CavnarAI

/// What these protect: the prices and photos an owner hands the app, and
/// the competitor refresh. A price typed with a grouping or decimal comma
/// must reach the server as the number the phone understood; rows the
/// server refuses must be named back; an applied invoice must not be applied
/// twice; an invoice photo must be downsampled before upload; one bad poll
/// must not abandon a refresh that is still running. XCTExpectFailure marks
/// confirmed CLIENT-31 / 39 / 41 / 49 / 50 / 60 defects; each flips when
/// fixed.
@MainActor
final class EdgeFoodCostIntelTests: XCTestCase {

    override func tearDown() async throws {
        MockURLProtocol.requestHandler = nil
    }

    // MARK: CLIENT-31 — quick count prices

    func testTheQuickCountSendsThePriceItUnderstood() async throws {
        let sentItems = Box<[[String: Any]]>([])
        let client = EdgeHTTP.client { request in
            if let items = EdgeHTTP.bodyJSON(request)?["items"] as? [[String: Any]] { sentItems.value = items }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "drift": [], "submitted_at": "2026-09-22"}"#)
        }
        let vm = FoodCostQuickEntryViewModel(client: client)
        // "1,234.50" in en_US; "3,50" on a comma-decimal keyboard. Either
        // way the phone parses a number the server's float() cannot.
        let typed = "1,234.50"
        let understood = try XCTUnwrap(FoodCostQuickEntryViewModel.parsedPrice(typed),
                                       "the phone accepts the row as priced")
        vm.items = [FoodCostItem(name: "Beef/Steak", unit: "lb", priceText: typed, usageText: "10")]
        await vm.submit()
        let price = try XCTUnwrap(sentItems.value.first?["price"])
        let asServerReadsIt = (price as? Double) ?? Double("\(price)")
        XCTExpectFailure("CLIENT-31: quick count sends priceText raw; the server's float(\"1,234.50\") fails and drops the row", strict: true) {
            XCTAssertEqual(asServerReadsIt, understood, "the server must receive the value the phone validated")
        }
    }

    func testAPlainPriceGoesThroughUnchanged() async throws {
        let sentItems = Box<[[String: Any]]>([])
        let client = EdgeHTTP.client { request in
            if let items = EdgeHTTP.bodyJSON(request)?["items"] as? [[String: Any]] { sentItems.value = items }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "drift": []}"#)
        }
        let vm = FoodCostQuickEntryViewModel(client: client)
        vm.items = [FoodCostItem(name: "Butter", unit: "lb", priceText: "4.25")]
        await vm.submit()
        XCTAssertEqual(sentItems.value.first?["price"] as? String, "4.25")
        XCTAssertTrue(vm.didSubmit)
    }

    func testRowsTheServerRefusedAreNamedBack() async {
        // _clean_quickcount_items answers ok:true with a `rejected` list for
        // rows it could not use; the app never decodes it.
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 200, """
            {"ok": true, "drift": [], "rejected": [{"name": "Butter", "why": "price is missing or not a number"}]}
            """)
        }
        let vm = FoodCostQuickEntryViewModel(client: client)
        vm.items = [FoodCostItem(name: "Butter", unit: "lb", priceText: "4.25"),
                    FoodCostItem(name: "Cream", unit: "qt", priceText: "3.10")]
        await vm.submit()
        XCTAssertTrue(vm.didSubmit)
        XCTExpectFailure("CLIENT-31: the `rejected` rows are never decoded, so they vanish with no message", strict: true) {
            XCTAssertTrue((vm.errorMessage ?? "").contains("Butter"))
        }
    }

    // MARK: CLIENT-39 — invoice photos

    private func jpeg(width: CGFloat, height: CGFloat) -> Data {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let image = UIGraphicsImageRenderer(size: CGSize(width: width, height: height), format: format).image { ctx in
            UIColor.white.setFill()
            ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        }
        return image.jpegData(compressionQuality: 0.5)!
    }

    func testAPreparedInvoicePhotoIsDownsampled() throws {
        let prepared = try XCTUnwrap(InvoiceScanViewModel.downscaledJPEG(jpeg(width: 4032, height: 3024)))
        let image = try XCTUnwrap(UIImage(data: prepared))
        XCTAssertLessThanOrEqual(max(image.size.width, image.size.height) * image.scale, 2000)
        XCTAssertLessThan(prepared.count, 4_500_000, "under the server's 4.5 MB upload cap")
    }

    func testASmallPhotoIsNotUpscaled() throws {
        let prepared = try XCTUnwrap(InvoiceScanViewModel.downscaledJPEG(jpeg(width: 800, height: 600)))
        let image = try XCTUnwrap(UIImage(data: prepared))
        XCTAssertEqual(image.size.width * image.scale, 800)
    }

    func testInvoicePhotosAreDecodedWithoutAFullSizeBitmap() throws {
        // A 48 MP photo decodes to ~190 MB through UIImage(data:) on the main
        // actor; memory pressure is not reproducible in a unit test, so the
        // downsampling API is checked at the source.
        let source = try EdgeSource.read("Features/FoodCost/InvoiceScanSheet.swift")
        let prep = try XCTUnwrap(EdgeSource.slice(source, from: "static func downscaledJPEG", length: 700))
        XCTExpectFailure("CLIENT-39: invoice photos are fully decoded with UIImage(data:) on the main actor", strict: true) {
            XCTAssertTrue(prep.contains("CGImageSourceCreateThumbnailAtIndex"))
            XCTAssertTrue(prep.contains("nonisolated"))
        }
    }

    // MARK: CLIENT-60 / CLIENT-50 — applying an invoice

    nonisolated private static let invoiceJSON = """
    {"id": 31, "supplier": "Sysco", "invoice_date": "2026-09-18", "duplicate": false, "applied_at": null,
     "lines": [{"index": 0, "description": "Butter 36ct", "ingredient_id": 4, "proposed_cost": 3.1, "selected": true}],
     "ingredients": [{"id": 4, "name": "Butter", "unit": "lb"}]}
    """

    private func scanned(_ vm: InvoiceScanViewModel) throws {
        let invoice = try JSONDecoder().decode(ScannedInvoice.self, from: Data(Self.invoiceJSON.utf8))
        vm.invoice = invoice
        vm.choices = [0: .init(include: true, ingredientId: 4, cost: "3.10")]
    }

    func testAnAppliedInvoiceIsNotAppliedAgain() async throws {
        let applies = Box(0)
        let client = EdgeHTTP.client { request in
            applies.value += 1
            if applies.value == 1 {
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "updated": [{"name": "Butter"}]}"#)
            }
            return EdgeHTTP.reply(request, 409, #"{"ok": false, "error": "This invoice was already applied."}"#)
        }
        let vm = InvoiceScanViewModel(client: client)
        try scanned(vm)
        await vm.apply()
        XCTAssertEqual(vm.appliedCount, 1)
        await vm.apply()        // the button is still there after success
        XCTExpectFailure("CLIENT-60: \"Update ticked costs\" stays after success; a second tap shows a red \"already applied\" under the success", strict: true) {
            XCTAssertEqual(applies.value, 1)
            XCTAssertNil(vm.errorMessage)
        }
    }

    func testAnExpiredSessionDuringApplyIsNotShownAsRawSystemText() async throws {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 401, #"{"ok": false, "error": "expired", "session_expired": true}"#)
        }
        let vm = InvoiceScanViewModel(client: client)
        try scanned(vm)
        await vm.apply()
        let shown = vm.errorMessage ?? ""
        XCTExpectFailure("CLIENT-50: error.localizedDescription on SessionExpiredError shows \"The operation couldn't be completed. (CavnarAI…)\"", strict: true) {
            XCTAssertFalse(shown.contains("CavnarAI."), shown)
            XCTAssertFalse(shown.lowercased().contains("operation couldn"), shown)
        }
    }

    // MARK: CLIENT-41 / 49 — Intel

    func testOneFailedIntelPollDoesNotAbandonTheRefresh() async {
        let polls = Box(0)
        let client = EdgeHTTP.client { request in
            let path = request.url?.path ?? ""
            if path == "/mobile/api/intel/refresh-competitors" {
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "job_id": "intel-1"}"#)
            }
            if path.hasPrefix("/mobile/api/intel/refresh-status/") {
                polls.value += 1
                return polls.value == 1
                    ? EdgeHTTP.reply(request, 502, "<html>Bad Gateway</html>")
                    : EdgeHTTP.reply(request, 200, #"{"ok": true, "status": "done"}"#)
            }
            return EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "summary not needed here"}"#)
        }
        let vm = IntelViewModel(client: client)
        await vm.refreshCompetitors()
        XCTAssertFalse(vm.isRefreshing)
        XCTExpectFailure("CLIENT-41: one poll error ends the competitor refresh with an error while the job keeps running", strict: true) {
            XCTAssertNil(vm.refreshError)
            XCTAssertEqual(polls.value, 2)
        }
    }

    func testACancelledIntelLoadSetsNoError() async {
        let client = EdgeHTTP.client { _ in throw URLError(.cancelled) }
        let vm = IntelViewModel(client: client)
        await vm.load()
        XCTExpectFailure("CLIENT-49: IntelViewModel.load reports a cancelled load as \"Couldn't load competitor intel.\"", strict: true) {
            XCTAssertNil(vm.errorMessage)
        }
    }

    func testACancelledScheduleHistoryLoadSetsNoError() async {
        let client = EdgeHTTP.client { _ in throw URLError(.cancelled) }
        let vm = ScheduleHistoryViewModel(client: client)
        await vm.load()
        XCTExpectFailure("CLIENT-49: ScheduleHistoryViewModel.load reports a cancelled load as an error", strict: true) {
            XCTAssertNil(vm.errorMessage)
        }
    }
}
