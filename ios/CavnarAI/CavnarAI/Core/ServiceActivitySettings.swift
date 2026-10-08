import ActivityKit
import SwiftUI

/// Account → Alerts → "On this phone": the owner's opt-in for "Tonight's
/// service" on the Lock Screen (parity audit #94). Device-local, like every
/// Live Activity: turning it on files this phone's push-to-start token so
/// the server can start tonight's activity at open; turning it off takes
/// the token back and ends a running one. Saves on the switch — there is no
/// Save button. Shown only to a login the "Tonight" route answers (Labor's
/// readers); the server refuses the token to anyone else too.
struct ServiceActivitySettingsSection: View {
    @State private var on = ServiceActivities.optedIn
    @State private var busy = false
    @State private var allowed = false

    private var enabledOnPhone: Bool { ActivityAuthorizationInfo().areActivitiesEnabled }

    var body: some View {
        Group {
            if allowed {
                AccountSection(kicker: "On this phone") {
                    AccountSwitchRow(
                        label: "Tonight\u{2019}s service on the Lock Screen",
                        detail: enabledOnPhone
                            ? "From open to close: how the night is running against a typical one, and who hasn\u{2019}t clocked in."
                            : "Live Activities are off for Cavnar AI in Settings \u{2192} Cavnar AI.",
                        isOn: Binding(get: { on }, set: { wanted in
                            on = wanted
                            busy = true
                            Haptic.light()
                            Task {
                                await ServiceActivities.setOptedIn(wanted)
                                busy = false
                            }
                        }),
                        busy: busy,
                        disabled: !enabledOnPhone,
                        showsDivider: false
                    )
                }
            }
        }
        .task { allowed = await ServiceActivities.mayWatch() }
    }
}

extension ServiceActivities {
    /// Whether this login reads tonight's service (the route's own check).
    /// Offline it is offered — the switch is this phone's to set.
    static func mayWatch(client: APIClient = .shared) async -> Bool {
        guard let bearer = Keychain.get(Keychain.Key.sessionToken), !bearer.isEmpty else { return false }
        do {
            let r: ServiceTonightResponse = try await client.sendWithBearer("/mobile/api/intraday/tonight",
                                                                            bearer: bearer)
            return r.ok
        } catch let error as APIClient.APIError where error.status == 403 || error.status == 404 {
            return false
        } catch {
            return true
        }
    }
}
