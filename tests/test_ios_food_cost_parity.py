"""The phone does what the web's Food Cost, export and Daily Report screens
do (web/iOS parity audit, 9/25/26). Source pins for the iOS side; the
server halves are tested in test_mobile_api, test_waste_trend and
test_edge_ai_food_cost."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")


def _swift(rel):
    return open(os.path.join(IOS, rel), encoding="utf-8").read()


def test_the_count_sheet_asks_about_a_delivery_that_arrived_mid_count():
    """The phone sent items alone, so the server never asked whether a
    delivery received after the sheet opened was in the count."""
    src = _swift("Features/FoodCost/CountSheetView.swift")
    assert 'case ledgerMark = "ledger_mark"' in src
    assert "SaveBody(items: lines, ledgerMark: ledgerMark, deliveries: deliveries)" in src
    assert "needsConfirm == true" in src and "error.status == 409" in src
    assert 'save(deliveries: "counted")' in src and 'save(deliveries: "after")' in src


def test_the_phone_logs_waste():
    src = _swift("Features/FoodCost/WasteLogForm.swift")
    assert '"/mobile/api/food-cost/waste"' in src
    # The server's reasons, and nothing else (client_api.WASTE_REASONS).
    import client_api
    for reason in client_api.WASTE_REASONS:
        assert f'("{reason}", ' in src
    assert "WasteLogForm(items: viewModel.items)" in _swift("Features/FoodCost/CountSheetView.swift")
    assert "case .waste: WasteLogSheet()" in _swift("Features/FoodCost/FoodCostQuickEntryView.swift")


def test_the_tracker_starts_from_the_saved_rows():
    src = _swift("Features/FoodCost/FoodCostQuickEntryViewModel.swift")
    assert '"/mobile/api/food-cost/tracker"' in src
    assert '"/mobile/api/food-cost/custom-item", method: .post' in src
    assert '"/mobile/api/food-cost/custom-item", method: .delete' in src
    assert "var isReadOnly: Bool { fromPantry && !isEditing }" in src


def test_pending_invoices_reopen_and_every_page_is_read():
    src = _swift("Features/FoodCost/InvoiceScanSheet.swift")
    assert '"/mobile/api/food-cost/invoices", hapticOnError: false' in src, "the pending list"
    assert '"/mobile/api/food-cost/invoices/\\(id)"' in src, "one invoice reopened"
    assert "var invoiceId: Int? = nil" in src
    assert 'mimeType: "application/pdf"' in src and "static func pagesPDF" in src
    assert "Read page 1 of" not in src, "the second page used to be dropped"
    assert '"/mobile/api/food-cost/ingredients"' in src, "new ingredient from a line"
    assert "ClaimKindTag(kind: verified ?" in src
    assert "case .invoice(let id): InvoiceScanSheet(invoiceId: id)" in _swift("Features/FoodCost/FoodCostQuickEntryView.swift")


def test_a_counts_only_login_gets_the_counts_screen():
    dest = _swift("Features/Modules/ModuleDestinationView.swift")
    assert 'ModuleAccess.shared.isCountsOnly("inventory")' in dest and "FoodCostCountsOnlyView(" in dest
    view = _swift("Features/FoodCost/FoodCostCountsOnlyView.swift")
    # Only FOOD_COST_ENTER paths: nothing that reads the margins.
    for forbidden in ("food-cost/analytics", "order-draft", "send-order", "food-cost/cfo"):
        assert forbidden not in view
    assert "showMoney: false" in view
    client = _swift("Core/APIClient.swift")
    assert 'case moduleForbidden = "module_forbidden"' in client
    assert "if moduleForbidden == true { return .moduleForbidden }" in client


def test_the_export_lists_only_the_accounts_scopes():
    src = _swift("Features/Account/AccountExportDataView.swift")
    assert "viewModel.summary?.data.exportScopes" in src
    assert "Self.scopeOptions.enumerated()" not in src


def test_coming_soon_does_not_claim_the_web_has_it():
    src = _swift("Features/Modules/ComingSoonView.swift")
    assert 'Text("This is available on desktop today' not in src
    assert "on the web or in the app" in src


def test_the_daily_report_has_a_period_view():
    vm = _swift("Features/DailyReport/DailyReportViewModel.swift")
    assert '"/mobile/api/dsr/period"' in vm and "case period(date: String?)" in vm
    assert "not read by the app yet" not in _swift("Features/DailyReport/DailyReportModels.swift")
    assert 'if kind == "period" { return .period(date: date) }' in _swift("Push/DeepLinkRouter.swift")
