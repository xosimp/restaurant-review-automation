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
        default:
            return nil
        }
    }
}

/// Scan invoice · Count · Order · More, at the top of Food Cost on both
/// sub-tabs (Friction audit #28, U3-7). Compact secondary buttons: the
/// screen's one primary stays the form's own submit.
struct FoodCostActionRow: View {
    var open: (FoodCostAction) -> Void

    var body: some View {
        HStack(spacing: 8) {
            item("Scan invoice", icon: "doc.text.viewfinder") {
                open(.scan(camera: DocumentCameraView.isAvailable))
            }
            item("Count", icon: "checklist") { open(.count) }
            item("Order", icon: "paperplane") { open(.order) }
            Menu {
                Button { open(.waste) } label: { Label("Log waste", systemImage: "trash") }
                Button { open(.dishes) } label: { Label("Your dishes", systemImage: "fork.knife") }
                Button { open(.recipes) } label: { Label("Recipes", systemImage: "list.bullet.clipboard") }
                Button { open(.margins) } label: { Label("Menu margins", systemImage: "chart.pie") }
                Button { open(.suppliers) } label: { Label("Suppliers", systemImage: "shippingbox") }
                Button { open(.scan(camera: false)) } label: { Label("Invoice from a photo or PDF", systemImage: "doc.richtext") }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(width: 44, height: 44)
                    .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                    .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                        .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
            }
            .accessibilityLabel("More")
            .simultaneousGesture(TapGesture().onEnded { Haptic.light() })
        }
    }

    private func item(_ label: String, icon: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            VStack(spacing: 4) {
                Image(systemName: icon).font(.system(size: 15, weight: .semibold))
                Text(label).font(.cavnarBody(12.5, weight: 700)).lineLimit(1).minimumScaleFactor(0.8)
            }
            .foregroundStyle(Color.cavnarInk)
            .frame(maxWidth: .infinity, minHeight: 44)
            .padding(.vertical, 6)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        }
        .buttonStyle(.plain)
    }
}
