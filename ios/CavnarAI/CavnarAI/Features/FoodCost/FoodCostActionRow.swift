import SwiftUI

/// What Food Cost's action row (and a nav path into Food Cost) can open.
enum FoodCostAction: Identifiable, Hashable {
    /// The invoice scanner; `camera` opens straight onto the camera.
    case scan(camera: Bool)
    /// One scanned invoice, reopened for its lines (a pending invoice, or
    /// the action queue's "invoice/<id>").
    case invoice(Int)
    case count
    case waste
    case order
    case recipes
    case margins
    /// Menu margins on one dish's price — a cost driver's "Look at X's
    /// price" (nav "inventory/menu?dish=X", parity audit #76).
    case menu(dish: String?)
    /// The pars the 86s say are too low — Analytics' par section
    /// (nav "inventory/pars"). Scrolled to, not a sheet.
    case pars
    /// The dish scorecard, "Your dishes" (parity audit #42).
    case dishes
    /// Who each ingredient is ordered from, with bulk assign (#78).
    case suppliers
    /// Orders sent and waiting to arrive, each received in place — the
    /// action row's "Receive" chip ("inventory/receive").
    case receive

    var id: String {
        switch self {
        case .scan(let camera): return camera ? "scan-camera" : "scan"
        case .invoice(let id): return "invoice-\(id)"
        case .count: return "count"
        case .waste: return "waste"
        case .order: return "order"
        case .recipes: return "recipes"
        case .margins: return "margins"
        case .menu(let dish): return "menu-\(dish ?? "")"
        case .pars: return "pars"
        case .dishes: return "dishes"
        case .suppliers: return "suppliers"
        case .receive: return "receive"
        }
    }

    /// "inventory/invoices?scan=camera" → the scanner on the camera;
    /// "inventory/order" → the supplier order; "inventory/count" → the
    /// count sheet; "inventory/waste" → log waste; "invoice/<id>" → that
    /// invoice's lines (it used to open a blank scanner, so the action
    /// queue's "1 invoice to check" led nowhere). Nil: the module itself.
    init?(path: NavPath) {
        let section = (path.head == "inventory" || path.head == "food") ? (path.target ?? "") : path.head
        if path.head == "invoice", let id = path.target.flatMap({ Int($0) }) {
            self = .invoice(id)
            return
        }
        switch section {
        case "invoices", "invoice", "scan":
            self = .scan(camera: path.query["scan"] == "camera" || section == "scan")
        case "order", "orders":
            self = .order
        case "count":
            self = .count
        case "waste":
            self = .waste
        case "recipes":
            self = .recipes
        case "margins":
            self = .margins
        case "menu":
            let dish = path.query["dish"]?.trimmingCharacters(in: .whitespaces)
            self = .menu(dish: (dish?.isEmpty ?? true) ? nil : dish)
        case "pars":
            self = .pars
        case "dishes", "scorecard":
            self = .dishes
        case "suppliers":
            self = .suppliers
        case "receive", "deliveries":
            self = .receive
        default:
            return nil
        }
    }
}

/// Scan · Count · Waste · Order · More, at the top of Food Cost on both
/// sub-tabs (Friction audit #28, U3-7) — the jobs done on the phone, in the
/// walk-in and at the back door. Waste came out of "More" (iOS readability
/// round, 10/8/26): a line of waste is a ten-second job. When deliveries
/// are waiting, a "Receive" chip sits under the row and opens them.
/// Compact secondary buttons: the screen's one primary stays its own.
struct FoodCostActionRow: View {
    /// Orders sent and not yet received (DeliveriesViewModel.waiting).
    var receiveCount: Int = 0
    var open: (FoodCostAction) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(spacing: CavnarSpace.xs) {
                item("Scan", icon: "doc.text.viewfinder", hint: "Scan an invoice") {
                    open(.scan(camera: DocumentCameraView.isAvailable))
                }
                item("Count", icon: "checklist", hint: "Count the walk-in") { open(.count) }
                item("Waste", icon: "trash", hint: "Log waste") { open(.waste) }
                item("Order", icon: "paperplane", hint: "Send an order") { open(.order) }
                Menu {
                    Button { open(.dishes) } label: { Label("Your dishes", systemImage: "fork.knife") }
                    Button { open(.recipes) } label: { Label("Recipes", systemImage: "list.bullet.clipboard") }
                    Button { open(.margins) } label: { Label("Menu margins", systemImage: "chart.pie") }
                    Button { open(.suppliers) } label: { Label("Suppliers", systemImage: "shippingbox") }
                    Button { open(.receive) } label: { Label("Deliveries", systemImage: "shippingbox.and.arrow.backward") }
                    Button { open(.scan(camera: false)) } label: { Label("Invoice from a photo or PDF", systemImage: "doc.richtext") }
                } label: {
                    Image(systemName: "ellipsis")
                        .font(.cavnar(.label))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(width: 44, height: 56)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                            .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                }
                .accessibilityLabel("More")
                .simultaneousGesture(TapGesture().onEnded { Haptic.light() })
            }
            if receiveCount > 0 {
                receiveChip
            }
        }
    }

    /// "2 deliveries waiting · Receive ›" — opens the deliveries directly.
    private var receiveChip: some View {
        Button {
            Haptic.light()
            open(.receive)
        } label: {
            HStack(spacing: CavnarSpace.xs) {
                Image(systemName: "shippingbox.fill")
                    .foregroundStyle(Color.cavnarAmber)
                    .accessibilityHidden(true)
                CavnarMixedText("\(receiveCount) deliver\(receiveCount == 1 ? "y" : "ies") waiting", role: .label)
                Spacer(minLength: CavnarSpace.xs)
                Text("Receive").cavnarText(.label, color: .cavnarEmber2)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarEmber2)
                    .accessibilityHidden(true)
            }
            .padding(.horizontal, CavnarSpace.s)
            .frame(maxWidth: .infinity, minHeight: 44)
            .background(Color.cavnarAmber.opacity(0.12), in: RoundedRectangle(cornerRadius: CavnarRadius.control))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarAmber.opacity(0.35), lineWidth: 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Receive \(receiveCount) deliver\(receiveCount == 1 ? "y" : "ies")")
    }

    private func item(_ label: String, icon: String, hint: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            VStack(spacing: CavnarSpace.xxs) {
                Image(systemName: icon).font(.cavnar(.label))
                Text(label).font(.cavnar(.label)).lineLimit(1).minimumScaleFactor(0.85)
            }
            .foregroundStyle(Color.cavnarInk)
            .frame(maxWidth: .infinity, minHeight: 56)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(hint)
    }
}
