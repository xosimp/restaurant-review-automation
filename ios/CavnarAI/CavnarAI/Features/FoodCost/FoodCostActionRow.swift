import SwiftUI

/// What Food Cost's action row (and a nav path into Food Cost) can open.
enum FoodCostAction: Identifiable, Hashable {
    /// The invoice scanner; `camera` opens straight onto the camera.
    case scan(camera: Bool)
    case count
    case order
    case recipes
    case margins

    var id: String {
        switch self {
        case .scan(let camera): return camera ? "scan-camera" : "scan"
        case .count: return "count"
        case .order: return "order"
        case .recipes: return "recipes"
        case .margins: return "margins"
        }
    }

    /// "inventory/invoices?scan=camera" → the scanner on the camera;
    /// "inventory/order" → the supplier order; "inventory/count" → the
    /// count sheet; "invoice/<id>" → the scanner. Nil: the module itself.
    init?(path: NavPath) {
        let section = path.head == "inventory" ? (path.target ?? "") : path.head
        switch section {
        case "invoices", "invoice", "scan":
            self = .scan(camera: path.query["scan"] == "camera" || section == "scan")
        case "order", "orders":
            self = .order
        case "count":
            self = .count
        case "recipes":
            self = .recipes
        case "margins":
            self = .margins
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
                Button { open(.recipes) } label: { Label("Recipes", systemImage: "list.bullet.clipboard") }
                Button { open(.margins) } label: { Label("Menu margins", systemImage: "chart.pie") }
                Button { open(.scan(camera: false)) } label: { Label("Invoice from a photo", systemImage: "photo") }
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
