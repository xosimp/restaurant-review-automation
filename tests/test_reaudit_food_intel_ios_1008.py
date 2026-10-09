"""Blind re-audit (10/8/26) of the iOS readability round — Food Cost (F*) and
Intel (I*), the phone halves. Source-level: each rule must hold on every
path, not only on the branch one fixture renders.

  F1   a reprice changes the live menu, so it is asked first (never one tap)
  F2   an invoice's ticked lines and typed costs are never lost to a swipe
  F11  "Received as ordered" waits out a short Undo, and commits on disappear
  F12  "Not right" on a recipe draft waits out a short Undo, commits on
       disappear; a draft is never "Dish #<id>"
  F13  a missing margin figure is a dash, never $0.00 / 0.0%
  F15  the walk-in's progress counts lines checked, not only lines changed
  I1   competitor rows judge "ahead"/"behind" only when the hero does
  I2   "Every question and answer" opens the web's AI visibility section
  I3   "Missed N of M" counts open questions only, as Details does
  I7   a recommendation's confidence is the server's, decoded, never invented
  I11  the website link opens the web's website card in Marketing
  I12  the website connection is set up on the web from Account too
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")


def _swift(*parts):
    with open(os.path.join(APP, *parts), encoding="utf-8") as f:
        return f.read()


def _dash():
    with open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def _between(src, start, end):
    return src[src.index(start):src.index(end, src.index(start))]


def test_f1_a_reprice_is_asked_before_it_changes_the_menu():
    fc = _swift("Features", "FoodCost", "FoodCostAnalyticsSection.swift")
    rows = _between(fc, "private func repriceRow(", "// The forecast pill mimics")
    assert "applyReprice(" not in rows, "no row sets a menu price on one tap"
    assert rows.count("confirmingReprice = PendingReprice(") == 2   # suggested and usual price
    assert ".confirmationDialog(confirmingReprice.map" in fc
    assert "await viewModel.applyReprice(pending.suggestion, price: pending.chosen)" in fc


def test_f2_the_invoice_sheet_guards_unsent_edits():
    inv = _swift("Features", "FoodCost", "InvoiceScanSheet.swift")
    assert ".interactiveDismissDisabled(viewModel.hasUnsavedEdits)" in inv
    assert "if viewModel.hasUnsavedEdits { confirmingLeave = true } else { dismiss() }" in inv
    assert "seededChoices = choices" in inv and "return choices != seededChoices" in inv


def test_f11_receive_as_ordered_has_an_undo_that_commits_on_disappear():
    d = _swift("Features", "FoodCost", "FoodCostDeliveries.swift")
    fn = _between(d, "func receiveAsOrdered(", "func undoReceive(")
    assert fn.index("Task.sleep") < fn.index("commitPending(order)"), "the post waits for the Undo window"
    assert ".onDisappear { viewModel.commitPendingReceives() }" in d
    assert "viewModel.receiveAsOrdered(order)" in d
    assert "Task { await viewModel.receive(order, withLines: false) }" not in d


def test_f12_not_right_has_an_undo_and_a_dish_is_named():
    r = _swift("Features", "FoodCost", "RecipeDraftsSheet.swift")
    fn = _between(r, "func rejectWithUndo(", "func undoReject(")
    assert "Task.sleep" in fn and "answer(" not in fn, "the delete waits for the Undo window"
    assert ".onDisappear { viewModel.commitPendingReject() }" in r
    assert "viewModel.rejectWithUndo(draft)" in r
    assert '"Dish #\\(' not in r and'"A dish without a name"' in r


def test_f13_a_missing_margin_is_a_dash():
    m = _swift("Features", "FoodCost", "MenuMarginsSheet.swift")
    assert not re.search(r"Self\.(money|pct)\(item\.\w+ \?\? 0\)", m)
    assert "item.plateCost ?? 0" not in m


def test_f15_walk_in_progress_counts_checked_lines():
    c = _swift("Features", "FoodCost", "CountSheetView.swift")
    walk = c[c.index("struct WalkInCountView"):]
    assert "viewModel.walkInProgressLine" in walk and "viewModel.checkedCount" in walk
    assert "if let old { viewModel.markChecked(old) }" in walk


def test_i1_competitor_rows_judge_only_when_the_hero_does():
    v = _swift("Features", "Intel", "IntelView.swift")
    assert "competitorRow(c, ownRating: summary.ownRating)" not in v
    assert v.count("competitorRow(c, ownRating: Self.comparableOwnRating(summary))") == 2
    fn = _between(v, "static func comparableOwnRating(", "/// Number at the given size")
    assert "summary.standing != nil ? own : nil" in fn and "summary.ratingsAreComparable ? own : nil" in fn


def test_i2_i11_web_links_land_on_their_sections():
    web = _dash()
    assert '<div id="in2-aiv" data-nav="intel/visibility">' in web
    assert 'id="mkt-web" data-nav="marketing/website"' in web
    aiv = _swift("Features", "Intel", "AIVisibilitySection.swift")
    assert 'path: "intel/visibility"' in aiv and 'path: "intel", actionLabel' not in aiv
    site = _swift("Features", "Intel", "WebsiteAnalyticsSection.swift")
    assert 'path: "marketing/website"' in site and 'path: "marketing", actionLabel' not in site


def test_i3_missed_counts_open_questions_only():
    aiv = _swift("Features", "Intel", "AIVisibilitySection.swift")
    fn = _between(aiv, "private func queriesSection(", "private func queryRow(")
    assert "let open = Self.openQueries(queries)" in fn and "open.filter { !$0.appeared }" in fn
    assert '$0.kind?.lowercased() != "branded"' in aiv


def test_i7_recommendation_confidence_is_decoded_never_invented():
    vm = _swift("Features", "Intel", "IntelViewModel.swift")
    assert "confidence = (try? c.decodeIfPresent(TrustConfidence.self, forKey: .confidence)) ?? nil" in vm
    v = _swift("Features", "Intel", "IntelView.swift")
    # Every ConfidenceLine on Intel is built from a decoded confidence.
    assert "if let c = rec.confidence {" in v and "top?.confidence.map {" in v
    assert "TrustConfidence(" not in v


def test_i12_account_sets_the_website_up_on_the_web():
    a = _swift("Features", "Account", "AccountConnectionsDetailView.swift")
    assert "WebsiteConnectSheet(" not in a
    assert 'path: "account/integrations", actionLabel: "Set up on the web"' in a
