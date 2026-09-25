import SwiftUI
import Observation

/// The pieces of chrome every screen can reach — the notification inbox
/// and the location switcher (friction audit #32, 9/25/26). Both used to
/// live on Home's toolbar only, so an owner in Labor had to go back to Home
/// to check an alert or change store, and nothing on a module screen said
/// which store its numbers belonged to. RootView owns one of these, presents
/// both sheets over every tab, and badges the Home tab with the unread count.
@Observable
@MainActor
final class AppChrome {
    var showingNotifications = false
    var showingLocationSwitcher = false
    let notificationsList = NotificationsListViewModel()
    let notificationsBadge = NotificationsBadgeViewModel()
    /// The location the session is on, and how many the login can switch
    /// between — read for owners only (the switcher is theirs). A module
    /// title names the location only when there is more than one.
    private(set) var locationName: String?
    private(set) var locationCount = 0

    var showsLocation: Bool { locationCount > 1 && !(locationName ?? "").isEmpty }

    /// Presents the inbox at once — its own skeleton covers a first load —
    /// and refreshes it underneath. The bell used to wait for the network
    /// before the sheet appeared.
    func openNotifications() {
        showingNotifications = true
        Task { await notificationsList.load() }
    }

    private struct LocationsResponse: Decodable {
        let ok: Bool
        let locations: [LocationOption]
    }

    /// The group's locations, for the title line and the switcher's reach.
    /// Silent on failure: the title simply doesn't name a location.
    func loadLocations(isOwner: Bool, client: APIClient = .shared) async {
        guard isOwner else {
            locationCount = 0
            locationName = nil
            return
        }
        guard let r: LocationsResponse = try? await client.send("/mobile/api/group-locations",
                                                                  hapticOnError: false), r.ok else { return }
        locationCount = r.locations.count
        locationName = r.locations.first(where: \.active)?.name
    }
}

private struct ShowsLocationTitleKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    /// Set on module screens (ModuleDestinationView): their title names the
    /// location under it for a multi-location owner, tappable to switch.
    var cavnarShowsLocationTitle: Bool {
        get { self[ShowsLocationTitleKey.self] }
        set { self[ShowsLocationTitleKey.self] = newValue }
    }
}

/// The bell for any screen's toolbar: opens the one inbox, with the unread
/// dot. Home and every module screen use the same one.
struct CavnarBellButton: View {
    @Environment(AppChrome.self) private var chrome: AppChrome?

    var body: some View {
        if let chrome {
            Button {
                Haptic.light()
                chrome.openNotifications()
            } label: {
                Image(systemName: "bell")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
                    // The dot sits on the 34pt disc's corner, inside the
                    // 44pt tap area the glass adds around it.
                    .frame(width: 34, height: 34)
                    .overlay(alignment: .topTrailing) {
                        if chrome.notificationsBadge.unreadCount > 0 {
                            CavnarAlertBadge(diameter: 8)
                                .offset(x: 2, y: -2)
                        }
                    }
                    .cavnarToolbarIconGlass()
            }
            .buttonStyle(.plain)
            .tint(nil)
            .accessibilityLabel(chrome.notificationsBadge.unreadCount > 0
                                ? "Notifications, \(chrome.notificationsBadge.unreadCount) unread"
                                : "Notifications")
        }
    }
}

/// A screen title — Clash, ink — and, on a module screen for an owner with
/// more than one location, the location's name under it as a button that
/// opens the switcher. `cavnarTitleToolbar` draws every title through this.
struct CavnarScreenTitle: View {
    let title: String
    @Environment(AppChrome.self) private var chrome: AppChrome?
    @Environment(\.cavnarShowsLocationTitle) private var showsLocation

    var body: some View {
        if showsLocation, let chrome, chrome.showsLocation, let name = chrome.locationName {
            VStack(spacing: 0) {
                Text(title)
                    .font(.cavnarHeadline(17))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                Button {
                    Haptic.light()
                    chrome.showingLocationSwitcher = true
                } label: {
                    HStack(spacing: 3) {
                        Text(name)
                            .font(.cavnarBody(11.5, weight: 600))
                            .lineLimit(1)
                        Image(systemName: "chevron.down")
                            .font(.system(size: 8, weight: .bold))
                    }
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(minHeight: 16)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Location: \(name). Switch location")
            }
        } else {
            Text(title)
                .font(.cavnarHeadline(18))
                .foregroundStyle(Color.cavnarInk)
                .lineLimit(1)
        }
    }
}
