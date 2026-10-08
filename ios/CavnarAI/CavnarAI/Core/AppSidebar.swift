import SwiftUI

/// What the iPad sidebar has selected (parity audit #99). The app's state
/// is still the tab (`AppTab`) and each tab's path; the sidebar is one more
/// way to set them, so the deep-link router, pushes and the command sheet
/// drive both shapes through the same `selectedTab` / `modulesPath`.
enum SidebarItem: Hashable {
    case tab(AppTab)
    /// A module's own screen, opened on a fresh Modules stack.
    case module(String)

    /// The row to highlight: a module while its screen is the one open in
    /// the Modules tab, else the tab itself.
    static func current(tab: AppTab, moduleKey: String?) -> SidebarItem {
        if tab == .modules, let moduleKey, !moduleKey.isEmpty { return .module(moduleKey) }
        return .tab(tab)
    }

    /// The tab this row lives in.
    var tab: AppTab {
        switch self {
        case .tab(let tab): return tab
        case .module: return .modules
        }
    }

    var moduleKey: String? {
        if case .module(let key) = self { return key }
        return nil
    }

    /// The modules listed under MODULES: the switched-on ones, in the
    /// server's order. A coming-soon module is never a destination.
    static func modules(_ all: [ModuleSummary]) -> [ModuleSummary] {
        all.filter(\.isAvailable)
    }
}

/// The owner app's sidebar on a regular width: Home and the Modules grid,
/// each module, Ask and Account — and, for an owner with more than one
/// location, which one is on screen with the switcher one tap away.
struct AppSidebar: View {
    @Binding var selection: SidebarItem?
    let modules: [ModuleSummary]
    /// Home carries the urgent count, as the tab bar's Home does.
    let urgentCount: Int
    /// Non-nil only for a multi-location owner.
    let locationName: String?
    var onSwitchLocation: () -> Void = {}

    var body: some View {
        List(selection: $selection) {
            Section {
                row(.tab(.home), AppTab.home.title, AppTab.home.systemImage)
                    .badge(urgentCount)
                row(.tab(.modules), AppTab.modules.title, AppTab.modules.systemImage)
            }
            let listed = SidebarItem.modules(modules)
            if !listed.isEmpty {
                Section {
                    ForEach(listed) { module in
                        row(.module(module.key), module.label, ModuleIcon.symbolName(for: module.icon))
                    }
                } header: {
                    kicker("MODULES")
                }
            }
            Section {
                row(.tab(.ask), AppTab.ask.title, AppTab.ask.systemImage)
                row(.tab(.account), AppTab.account.title, AppTab.account.systemImage)
            }
            if let locationName {
                Section {
                    Button {
                        Haptic.light()
                        onSwitchLocation()
                    } label: {
                        HStack(spacing: 10) {
                            Image(systemName: "mappin.and.ellipse")
                                .font(.system(size: 15, weight: .semibold))
                                .foregroundStyle(Color.cavnarEmber)
                            Text(locationName)
                                .font(.cavnarBody(15, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                                .lineLimit(1)
                            Spacer(minLength: 6)
                            Image(systemName: "chevron.down")
                                .font(.system(size: 10, weight: .bold))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Location, \(locationName)")
                    .accessibilityHint("Switches location")
                } header: {
                    kicker("LOCATION")
                }
            }
        }
        .listStyle(.sidebar)
        .scrollContentBackground(.hidden)
        .background(Color.cavnarChrome.ignoresSafeArea())
        .navigationTitle("Cavnar AI")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Cavnar AI") }
    }

    private func row(_ item: SidebarItem, _ title: String, _ symbol: String) -> some View {
        Label {
            Text(title)
                .font(.cavnarBody(15, weight: 600))
                .foregroundStyle(Color.cavnarInk)
        } icon: {
            Image(systemName: symbol)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber)
        }
        .tag(item)
    }

    private func kicker(_ text: String) -> some View {
        Text(text)
            .font(.cavnarBody(11.5, weight: 700))
            .tracking(1.2)
            .foregroundStyle(Color.cavnarEmber)
    }
}
