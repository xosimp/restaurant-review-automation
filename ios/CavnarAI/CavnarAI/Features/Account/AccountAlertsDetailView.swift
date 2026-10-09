import SwiftUI
import UIKit

/// Account → Notifications: what reaches THIS login's phone (iOS
/// readability round [24], "Web explains. iPhone decides."). The phone
/// keeps the choices an owner makes about their own phone — whether iOS
/// lets Cavnar AI through at all, push to my phone, what's muted on it, my
/// own quiet hours, my morning brief, how much to hear from Cavnar AI, a
/// test notification, and the "you never open X — mute it" nudge. Every
/// one of them saves the moment it changes; there is no Save button here.
///
/// The restaurant's alert rules — which alerts fire, text and email
/// delivery, extra emails, the restaurant's quiet hours, the weekly digest,
/// email preferences, issue routing and the alert contacts — are one web
/// row ("Restaurant alert rules · Edit on the web"). They were 40-odd
/// controls on the phone behind a Save button at the bottom that Back
/// silently discarded.
struct AccountAlertsDetailView: View {
    let viewModel: AccountViewModel
    @Environment(SessionStore.self) private var sessionStore
    /// A morning-brief change the server did not take — it is rolled back
    /// and said here. The save was `try?`, so a refusal looked saved.
    @State private var briefError: String?

    @State private var pushDenied = false
    @State private var pushUndetermined = false
    @State private var brief = BriefSettings()
    @State private var briefLoaded = false
    @State private var testPushLabel: String?
    @State private var sendingTestPush = false

    /// The restaurant's alert rules are the account owner's (403 owner_only
    /// for anyone else) — the web row says so to a teammate.
    private var isOwner: Bool { sessionStore.currentUser?.isOwner == true }

    /// The contacts on screen after "Add someone to text" saved one: the
    /// draft as the owner left it (names and numbers typed and not yet
    /// saved stay), a consent the server just recorded on one of them taken
    /// from the server, and each contact the server has that it did not
    /// have before (`before`) appended. An empty new row left blank gives
    /// way, so the list never passes the two the server keeps.
    static func mergeAddedContacts(draft: [AlertContact], before: Set<Int>,
                                   server: [AlertContact]) -> [AlertContact] {
        let onScreen = Set(draft.map(\.id))
        let added = server.filter { !before.contains($0.id) && !onScreen.contains($0.id) }
        var out: [AlertContact] = draft.compactMap { local in
            if local.id < 0, !added.isEmpty,
               local.name.trimmingCharacters(in: .whitespaces).isEmpty,
               local.phone.filter(\.isNumber).isEmpty {
                return nil
            }
            guard let s = server.first(where: { $0.id == local.id }), s.smsConsent != local.smsConsent else {
                return local
            }
            return AlertContact(id: local.id, name: local.name, phone: local.phone, smsConsent: s.smsConsent)
        }
        out.append(contentsOf: added)
        return out
    }

    /// `alerts` is the restaurant's rules as Account loaded them — edited
    /// on the web now, so the phone only reads this login's own settings.
    init(viewModel: AccountViewModel, alerts: AccountAlerts) {
        self.viewModel = viewModel
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    hero

                    // A denial is permanent and silent — iOS will not show
                    // the system prompt a second time, so an owner who
                    // tapped "Don't Allow" once was unreachable by push
                    // forever with nothing anywhere saying why.
                    if pushDenied || pushUndetermined {
                        permissionCard
                    }

                    // This login's own choices (push, muted types, quiet
                    // hours, its brief, the never-opened nudge) — memory
                    // round 9/29/26 (M2 owner_layers). Each saves on change.
                    AccountMyNotifications()

                    // "Is push actually working on my phone?" This login's
                    // own devices only — a test that buzzes a manager's
                    // phone is not a test.
                    VStack(alignment: .leading, spacing: 0) {
                        AccountActionRow(
                            label: "Send me a test notification",
                            detail: testPushLabel ?? "Goes to this phone only.",
                            symbol: "paperplane.fill",
                            busy: sendingTestPush,
                            showsDivider: false
                        ) { Task { await sendTestPush() } }
                    }
                    .accountCard()

                    // The one dial for "too much" or "too little" (the web's
                    // How much to hear from Cavnar AI, density audit #38) —
                    // briefing_level on the /morning-brief/settings twin.
                    // Saves on change, rolled back if the server refuses.
                    if briefLoaded && brief.canEdit {
                        AccountSection(kicker: "How much to hear from Cavnar AI") {
                            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                                CavnarSegmentedControl(
                                    selection: Binding(get: { brief.briefingLevel },
                                                       set: { level in
                                                           guard level != brief.briefingLevel else { return }
                                                           let before = brief
                                                           brief.briefingLevel = level
                                                           saveBrief(rollback: before)
                                                       }),
                                    options: Self.levels,
                                    accessibilityTitle: "Morning brief detail"
                                ) { Self.levelLabel($0) }
                                Text(Self.levelNote(brief.briefingLevel))
                                    .cavnarText(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .padding(.vertical, 9)
                        }
                    }
                    if let briefError {
                        Text(briefError).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    // L3: the restaurant's rules, for everyone, on the web.
                    VStack(alignment: .leading, spacing: 0) {
                        CavnarWebLinkRow(
                            title: "Restaurant alert rules",
                            subtitle: isOwner
                                ? "Which alerts fire, text and email, quiet hours, the weekly digest, issue texts and alert contacts."
                                : "What your restaurant is set to. Only the account owner can change these.",
                            path: "account/notifications",
                            actionLabel: isOwner ? "Edit on the web" : "Open on the web"
                        )
                    }
                    .accountCard()
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("Notifications")
            .task {
                await PushManager.shared.refreshAuthorization()
                pushDenied = PushManager.shared.authorizationDenied
                pushUndetermined = PushManager.shared.authorizationUndetermined
                await loadBrief()
            }
        }
    }

    // MARK: - Identity

    private var hero: some View {
        AccountHero(title: pushDenied ? "Notifications are off" : "Your notifications") {
            GlowBadge(systemImage: pushDenied ? "bell.slash" : "bell.badge", size: 64)
        } subtitle: {
            Text(pushDenied ? "Turn them on in Settings to hear from Cavnar AI."
                            : "What reaches your phone. Each change saves at once.")
        }
    }

    // MARK: - iOS permission

    private var permissionCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            if pushDenied {
                CavnarCaveat(
                    title: "Notifications are turned off for Cavnar AI",
                    detail: "iOS won't ask again, so nothing below can reach your phone until you turn them back on in Settings."
                )
                .padding(.vertical, 9)
                AccountActionRow(label: "Open Settings",
                                 detail: "Notifications \u{2192} Cavnar AI \u{2192} Allow Notifications",
                                 symbol: "arrow.up.forward", showsDivider: false) {
                    if let url = URL(string: UIApplication.openSettingsURLString) {
                        UIApplication.shared.open(url)
                    }
                }
            } else {
                // The system prompt waits for the second app open so it
                // doesn't fire before the owner has seen a single number.
                // Without this row that deferral is a trap on a fresh
                // install: no prompt, no token, and nothing offering either.
                CavnarCaveat(
                    title: "Nothing can reach your phone yet",
                    detail: "Cavnar AI hasn't asked for permission to send notifications. None of the switches below do anything until it has."
                )
                .padding(.vertical, 9)
                AccountActionRow(label: "Allow notifications",
                                 detail: "Asks iOS now.",
                                 symbol: "bell.badge", showsDivider: false) {
                    Task {
                        await PushManager.shared.promptNow()
                        pushDenied = PushManager.shared.authorizationDenied
                        pushUndetermined = PushManager.shared.authorizationUndetermined
                    }
                }
            }
        }
        .accountCard()
    }

    // MARK: - Test push

    private struct TestPushResponse: Decodable {
        let ok: Bool
        let sent: Int?
        let error: String?
    }

    private func sendTestPush() async {
        sendingTestPush = true
        defer { sendingTestPush = false }
        do {
            let response: TestPushResponse = try await APIClient.shared.send(
                "/mobile/api/account/send-test-push", method: .post)
            if response.ok {
                Haptic.success()
                testPushLabel = "Sent \u{2014} it should arrive in a second"
            } else {
                testPushLabel = response.error ?? "Apple did not accept it."
            }
        } catch let error as APIClient.APIError {
            testPushLabel = error.message
        } catch {
            testPushLabel = "Couldn't send a test notification."
        }
    }

    // MARK: - Morning brief & issues

    /// The restaurant's brief settings, from the same /morning-brief
    /// twin the web dashboard reads. Kept separate from `draft` because
    /// they are a different endpoint with a different permission: only a
    /// principal may change them (`canEdit`).
    struct BriefSettings: Decodable, Equatable {
        var enabled = true
        var hour = 7
        var holdAlerts = true
        var preshiftNudgeHour = 0
        /// calm | normal | all — how much reaches the owner besides the brief.
        var briefingLevel = "normal"
        var canEdit = false

        private enum Outer: String, CodingKey { case settings, canEdit = "can_edit" }
        private enum Inner: String, CodingKey {
            case enabled, hour
            case holdAlerts = "hold_alerts"
            case preshiftNudgeHour = "preshift_nudge_hour"
            case briefingLevel = "briefing_level"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let outer = try decoder.container(keyedBy: Outer.self)
            canEdit = (try? outer.decode(Bool.self, forKey: .canEdit)) ?? false
            let inner = try outer.nestedContainer(keyedBy: Inner.self, forKey: .settings)
            enabled = (try? inner.decode(Bool.self, forKey: .enabled)) ?? true
            hour = (try? inner.decode(Int.self, forKey: .hour)) ?? 7
            holdAlerts = (try? inner.decode(Bool.self, forKey: .holdAlerts)) ?? true
            preshiftNudgeHour = (try? inner.decode(Int.self, forKey: .preshiftNudgeHour)) ?? 0
            let level = (try? inner.decode(String.self, forKey: .briefingLevel)) ?? "normal"
            briefingLevel = AccountAlertsDetailView.levels.contains(level) ? level : "normal"
        }
    }

    private struct BriefPayload: Encodable {
        let enabled: Bool
        let hour: Int
        let hold_alerts: Bool
        let preshift_nudge_hour: Int
        let briefing_level: String
    }

    static let levels = ["calm", "normal", "all"]

    static func levelLabel(_ level: String) -> String {
        switch level {
        case "calm": return "Calm"
        case "all": return "Everything"
        default: return "Normal"
        }
    }

    /// The web dial's notes (dashboard.html _LEVEL_NOTES), word for word.
    static func levelNote(_ level: String) -> String {
        switch level {
        case "calm": return "The brief, results and the close. Nothing else."
        case "all": return "Everything Cavnar AI has to say, as it happens."
        default: return "Adds the pre-dinner pulse, coverage and opportunities, up to four a day."
        }
    }

    // MARK: - Issue routing

    /// GET /issues/routing — who an issue is texted to, and who it goes to
    /// when nobody responds, over this restaurant's alert contacts. Only a
    /// principal gets it (403 otherwise, and the rows stay hidden).
    struct IssueRouting: Decodable {
        struct Route: Decodable {
            let contactId: Int
            let name: String?
            let escalateAfterMinutes: Int?
            enum CodingKeys: String, CodingKey {
                case name
                case contactId = "contact_id"
                case escalateAfterMinutes = "escalate_after_minutes"
            }
        }
        struct Contact: Decodable, Hashable {
            let id: Int
            let name: String
            let smsConsent: Bool
            enum CodingKeys: String, CodingKey { case id, name; case smsConsent = "sms_consent" }
        }
        var routing: [String: Route]
        var contacts: [Contact]
        /// Texts on or off for new issues (Erik, 10/5/26): off, issues still
        /// open and are assigned, and reach people by push and the bell.
        /// Nil from an older server — the switch isn't drawn then.
        var issueTexts: Bool?

        private enum CodingKeys: String, CodingKey { case routing, contacts; case issueTexts = "issue_texts" }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            routing = (try? c.decode([String: Route].self, forKey: .routing)) ?? [:]
            contacts = (try? c.decode([Contact].self, forKey: .contacts)) ?? []
            issueTexts = c.setupBool(.issueTexts)
        }
    }

    /// The web's 1–5 star box, as half-star steps.
    static let ratingFloors: [Double] = [3.0, 3.5, 4.0, 4.2, 4.4, 4.5, 4.6, 4.8]

    /// The steps, plus a stored floor that isn't one of them (set on the web).
    static func ratingOptions(_ stored: Double?) -> [Double] {
        Array(Set(ratingFloors + [stored ?? 4.0])).sorted()
    }

    static func starLabel(_ v: Double) -> String {
        (v == v.rounded() ? String(Int(v)) : String(format: "%.1f", v)) + "\u{2605}"
    }

    private func loadBrief() async {
        // The brief itself rides along in this response; only the settings
        // are wanted here, and a failure leaves the section hidden rather
        // than showing switches that would not save.
        if let loaded: BriefSettings = try? await APIClient.shared.send("/mobile/api/morning-brief") {
            brief = loaded
        }
        briefLoaded = true
    }

    /// Saves the brief as it now reads. A refusal or a dropped connection
    /// puts `before` back (unless another change has landed since) and
    /// says why — it used to be `try?`, so a failed save looked saved.
    private func saveBrief(rollback before: BriefSettings) {
        let attempted = brief
        let payload = BriefPayload(enabled: brief.enabled, hour: brief.hour,
                                   hold_alerts: brief.holdAlerts,
                                   preshift_nudge_hour: brief.preshiftNudgeHour,
                                   briefing_level: brief.briefingLevel)
        briefError = nil
        Task {
            let failure: String?
            do {
                let r: APIClient.OKResponse = try await APIClient.shared.send(
                    "/mobile/api/morning-brief/settings", method: .post, body: payload)
                failure = r.ok ? nil : (r.error ?? Self.briefSaveFailed)
            } catch let error as APIClient.APIError {
                failure = error.message
            } catch {
                failure = Self.briefSaveFailed
            }
            guard let failure else { return }
            if brief == attempted { brief = before }
            briefError = failure
        }
    }

    static let briefSaveFailed = "Couldn't save the morning brief settings. Check your connection and try again."
}
