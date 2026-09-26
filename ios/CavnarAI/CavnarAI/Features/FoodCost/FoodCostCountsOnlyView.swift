import SwiftUI

/// Counts-only Food Cost (friction audit U2-27) — the web's "Counts" tab
/// for a login with FOOD_COST_ENTER but not FOOD_COST_VIEW: a manager who
/// counts the walk-in, logs waste and receives deliveries without seeing
/// the margins. The tile says so (`mode: "counts"`, ModuleAccess); the full
/// screen used to open instead, and every analytics and order call came
/// back 403. Everything here is a FOOD_COST_ENTER path, and none of them
/// returns a dollar.
struct FoodCostCountsOnlyView: View {
    var focus: NavPath? = nil
    @State private var countSheet = CountSheetViewModel()
    @State private var deliveries = DeliveriesViewModel()
    @State private var sheet: Sheet?
    @State private var focusSpent = false

    private enum Sheet: String, Identifiable {
        case count, waste
        var id: String { rawValue }
    }

    init(focus: NavPath? = nil) {
        self.focus = focus
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("FOOD COST · STOCK")
                        .font(.cavnarBody(CavnarType.kicker, weight: 700))
                        .tracking(1.2)
                        .foregroundStyle(Color.cavnarEmber2)
                    Text("Counts & deliveries")
                        .font(.cavnarHeadline(22))
                        .foregroundStyle(Color.cavnarInk)
                    HomeMixedText.make(statusLine, size: 14, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Count what\u{2019}s on hand, log what was thrown out, and receive what arrived. Costs and margins stay with the owner.")
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .cavnarCard()

                HStack(spacing: 10) {
                    Button {
                        Haptic.light()
                        sheet = .count
                    } label: {
                        Label("Count", systemImage: "checklist").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    Button {
                        Haptic.light()
                        sheet = .waste
                    } label: {
                        Label("Log waste", systemImage: "trash").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }

                DeliveriesSection(viewModel: deliveries, showMoney: false, title: "WAITING TO ARRIVE")
            }
            .padding(20)
        }
        .scrollDismissesKeyboard(.immediately)
        .cavnarModuleBackground()
        .navigationTitle("Food Cost")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Food Cost") }
        .cavnarEmberRefreshable { await reload() }
        .task { await reload() }
        // "inventory/count" and "inventory/waste" open their sheet, a beat
        // after the push lands (a sheet asked for mid-push is dropped).
        .task {
            guard !focusSpent, let focus else { return }
            focusSpent = true
            let section = (focus.head == "inventory" || focus.head == "food") ? (focus.target ?? "") : focus.head
            let target: Sheet? = section == "count" ? .count : (section == "waste" ? .waste : nil)
            guard let target else { return }
            try? await Task.sleep(for: .milliseconds(450))
            sheet = target
        }
        .sheet(item: $sheet, onDismiss: { Task { await reload() } }) { which in
            switch which {
            case .count: CountSheetView()
            case .waste: WasteLogSheet()
            }
        }
    }

    /// "Last counted 9/22/26 · 2 deliveries to receive" — the web's status.
    private var statusLine: String {
        let waiting = deliveries.waiting.count
        let tail = waiting == 0 ? "nothing waiting to arrive"
            : "\(waiting) deliver\(waiting == 1 ? "y" : "ies") to receive"
        return countSheet.lastCountedLine + " · " + tail
    }

    private func reload() async {
        async let a: Void = countSheet.load()
        async let b: Void = deliveries.load()
        _ = await (a, b)
    }
}
