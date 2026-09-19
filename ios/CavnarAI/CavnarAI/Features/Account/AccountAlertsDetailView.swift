import SwiftUI
import UIKit

/// Pushed from Account's "Alerts & digest" row. The old inline card only
/// exposed 7 of the 12 settings the backend actually stores (missing
/// alert_health, alert_negative_trend, alert_no_response, urgent_via_email,
/// and digest_day entirely) and showed alert contacts read-only even
/// though the save endpoint has always accepted a full replacement list.
/// Also the first UI anywhere for alert_quiet_start/end — notify.py has
/// checked these since is_in_quiet_hours() existed, but neither client
/// ever gave an owner a way to actually set them.
struct AccountAlertsDetailView: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var postedLabel: String?

    @State private var draft: AlertSettings
    @State private var contacts: [AlertContact]
    @State private var quietHoursEnabled: Bool
    @State private var quietStart: Date
    @State private var quietEnd: Date
    @State private var testDigestLabel: String?
    @State private var pushDenied = false
    @State private var brief = BriefSettings()
    @State private var briefLoaded = false
    @State private var nudge: EngagementSuggestion?
    @State private var testPushLabel: String?
    @State private var sendingTestPush = false
    private enum AlertsField: Hashable { case extraEmails, contactName(Int), contactPhone(Int) }
    @FocusState private var focusedField: AlertsField?

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()

    private static let days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    init(viewModel: AccountViewModel, alerts: AccountAlerts) {
        self.viewModel = viewModel
        _draft = State(initialValue: alerts.settings)
        _contacts = State(initialValue: alerts.contacts)
        _quietHoursEnabled = State(initialValue: alerts.settings.alertQuietStart != nil && alerts.settings.alertQuietEnd != nil)
        _quietStart = State(initialValue: Self.timeFormatter.date(from: alerts.settings.alertQuietStart ?? "")
            ?? Calendar.current.date(bySettingHour: 22, minute: 0, second: 0, of: Date())!)
        _quietEnd = State(initialValue: Self.timeFormatter.date(from: alerts.settings.alertQuietEnd ?? "")
            ?? Calendar.current.date(bySettingHour: 8, minute: 0, second: 0, of: Date())!)
    }

    var body: some View {
        NavigationStack {
        ScrollViewReader { proxy in
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                hero
                statusStrip

                AccountSection(kicker: "What triggers an alert") {
                    AccountSwitchRow(label: "1-star reviews", isOn: $draft.alert1star)
                    AccountSwitchRow(label: "2-star reviews", isOn: $draft.alert2star)
                    AccountSwitchRow(label: "5-star reviews", isOn: $draft.alert5star)
                    AccountSwitchRow(label: "Health or safety mention", isOn: $draft.alertHealth)
                    AccountSwitchRow(label: "Negative review spike", isOn: $draft.alertNegSpike)
                    AccountSwitchRow(label: "Rating declining trend", isOn: $draft.alertNegativeTrend)
                    AccountSwitchRow(label: "Unresponded review (48h)", isOn: $draft.alertNoResponse)
                    AccountSwitchRow(label: "Labor over target", isOn: $draft.alertLaborOver)
                    AccountSwitchRow(label: "Food waste flagged", isOn: $draft.alertFoodWaste)
                    AccountSwitchRow(label: "AI visibility drops", isOn: $draft.alertAiVisibilityDrop)
                    masterAlertPill.padding(.vertical, 9)
                }

                AccountSection(kicker: "How urgent alerts reach you") {
                    AccountSwitchRow(label: "Text alerts", isOn: $draft.urgentViaSms)
                    AccountSwitchRow(label: "Email alerts", isOn: $draft.urgentViaEmail)
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Also email").font(.cavnarBody(16)).foregroundStyle(Color.cavnarInk3)
                        TextField("chef@…, gm@… (up to 3)", text: $draft.alertExtraEmails)
                            .font(.cavnarBody(16, weight: 700))
                            .foregroundStyle(Color.cavnarInk)
                            .keyboardType(.emailAddress)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .focused($focusedField, equals: .extraEmails)
                            .id("alerts-extra-emails")
                        Text("Alert and digest emails also go to these addresses.")
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3.opacity(0.8))
                    }
                    .padding(.vertical, 9)
                }

                AccountSection(kicker: "Push notifications") {
                    // A denial is permanent and silent — iOS will not show
                    // the system prompt a second time, so an owner who
                    // tapped "Don't Allow" once was unreachable by push
                    // forever with nothing anywhere saying why. Every switch
                    // below it would have been a lie.
                    if pushDenied {
                        CavnarCaveat(
                            title: "Notifications are turned off for Cavnar AI",
                            detail: "iOS won't ask again, so nothing below can reach your phone until you turn them back on in Settings."
                        )
                        AccountActionRow(label: "Open Settings",
                                         detail: "Notifications → Cavnar AI → Allow Notifications",
                                         symbol: "arrow.up.forward") {
                            if let url = URL(string: UIApplication.openSettingsURLString) {
                                UIApplication.shared.open(url)
                            }
                        }
                    }
                    // One sentence about what is being sent and never
                    // opened. A suggestion, never an automatic change:
                    // reading a banner leaves no tap behind, so switching
                    // alerts off on tap data alone would quietly silence
                    // ones an owner reads every day.
                    if let nudge {
                        CavnarCaveat(
                            title: "\(nudge.delivered) \u{201C}\(nudge.label)\u{201D} alerts in the last \(nudge.days) days",
                            detail: "You haven't opened one of them. Want to stop pushing these to your phone?"
                        )
                        AccountActionRow(label: "Stop pushing these",
                                         detail: "Still logged, still in your notifications list.",
                                         symbol: "bell.slash") {
                            applyNudge(nudge)
                        }
                    }
                    pushRow("1-star reviews", $draft.al1starPush, on: draft.alert1star)
                    pushRow("2-star reviews", $draft.al2starPush, on: draft.alert2star)
                    pushRow("5-star reviews", $draft.al5starPush, on: draft.alert5star)
                    pushRow("Health or safety mention", $draft.alHealthPush, on: draft.alertHealth)
                    pushRow("Negative review spike", $draft.alSpikePush, on: draft.alertNegSpike)
                    pushRow("Unresponded review (48h)", $draft.alUnresPush, on: draft.alertNoResponse)
                    AccountSwitchRow(label: "Play a sound", isOn: $draft.pushSound)
                    // "Is push actually working on my phone?" had no answer
                    // short of reaching into the database. This login's own
                    // devices only — a test that buzzes a manager's phone
                    // is not a test.
                    AccountActionRow(
                        label: "Send me a test notification",
                        detail: testPushLabel ?? "Goes to this phone only.",
                        symbol: "paperplane.fill",
                        busy: sendingTestPush
                    ) { Task { await sendTestPush() } }
                    Text("Push doesn't need text/email alerts turned on — it's free to send, so it's gated per-alert-type here instead. Trend, labor, waste and visibility alerts push automatically once enabled.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3.opacity(0.8))
                        .padding(.vertical, 9)
                }

                // The morning brief had no settings screen on the phone at
                // all — an owner who runs Cavnar from iOS could not change
                // the hour it arrives, stop alerts buzzing mid-service, or
                // turn on the lineup nudge. All three are the same
                // /morning-brief/settings twin the web dashboard uses.
                if briefLoaded && brief.canEdit {
                    AccountSection(kicker: "Morning brief & issues") {
                        AccountSwitchRow(
                            label: "Morning brief",
                            detail: "Yesterday, what to fix first, and what is waiting on you — to your phone, or email if the app isn't installed.",
                            isOn: Binding(get: { brief.enabled },
                                          set: { brief.enabled = $0; saveBrief() })
                        )
                        AccountKVRow(label: "Send it at") {
                            Picker("", selection: Binding(get: { brief.hour },
                                                          set: { brief.hour = $0; saveBrief() })) {
                                ForEach(4..<12, id: \.self) { Text(Self.hourLabel($0)).tag($0) }
                            }
                            .labelsHidden().tint(Color.cavnarEmber)
                        }
                        AccountSwitchRow(
                            label: "Hold alerts through service",
                            detail: "A two-star review at 12:15 can't be acted on until the rush is over. Held alerts arrive when it ends; health mentions never wait.",
                            isOn: Binding(get: { brief.holdAlerts },
                                          set: { brief.holdAlerts = $0; saveBrief() })
                        )
                        AccountKVRow(label: "Lineup notes to the manager", showsDivider: false) {
                            Picker("", selection: Binding(get: { brief.preshiftNudgeHour },
                                                          set: { brief.preshiftNudgeHour = $0; saveBrief() })) {
                                Text("Off").tag(0)
                                ForEach(12..<21, id: \.self) { Text(Self.hourLabel($0)).tag($0) }
                            }
                            .labelsHidden().tint(Color.cavnarEmber)
                        }
                    }
                }

                AccountSection(kicker: "Quiet hours") {
                    AccountSwitchRow(
                        label: "Pause overnight",
                        detail: "Text, email, and push all wait until your quiet window ends",
                        isOn: $quietHoursEnabled,
                        showsDivider: quietHoursEnabled
                    )
                    if quietHoursEnabled {
                        AccountKVRow(label: "From") {
                            DatePicker("", selection: $quietStart, displayedComponents: .hourAndMinute).labelsHidden().tint(Color.cavnarEmber)
                        }
                        AccountKVRow(label: "Until") {
                            DatePicker("", selection: $quietEnd, displayedComponents: .hourAndMinute).labelsHidden().tint(Color.cavnarEmber)
                        }
                        AccountSwitchRow(
                            label: "Health alerts break through",
                            detail: "A health or safety mention still reaches you during quiet hours, and pushes through Focus modes",
                            isOn: $draft.alertHealthBypassQuiet,
                            showsDivider: false
                        )
                    }
                }

                AccountSection(kicker: "Weekly digest") {
                    AccountSwitchRow(label: "Weekly digest", isOn: $draft.digestEnabled, showsDivider: draft.digestEnabled)
                    if draft.digestEnabled {
                        AccountKVRow(label: "Delivered on") {
                            Picker("", selection: $draft.digestDay) {
                                ForEach(Self.days, id: \.self) { day in
                                    Text(day.capitalized).tag(day)
                                }
                            }
                            .tint(Color.cavnarEmber)
                        }
                        AccountActionRow(
                            label: "Send me a preview",
                            detail: testDigestLabel ?? viewModel.testDigestError,
                            symbol: "paperplane.fill",
                            busy: viewModel.isSendingTestDigest,
                            showsDivider: false
                        ) {
                            Task {
                                await viewModel.sendTestDigest()
                                if viewModel.testDigestSucceeded {
                                    Haptic.success()
                                    testDigestLabel = "Preview sent"
                                }
                            }
                        }
                    }
                }

                AccountSection(kicker: "Email preferences") {
                    AccountSwitchRow(
                        label: "Product updates & tips",
                        detail: "Never affects security emails — sign-in alerts, 2FA codes, and password changes always go out",
                        isOn: Binding(
                            get: { viewModel.summary?.account.marketingEmailsOptOut == false },
                            set: { on in Task { await viewModel.toggleMarketingOptOut(!on) } }
                        ),
                        busy: viewModel.isTogglingMarketingOptOut,
                        showsDivider: false
                    )
                }

                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .center) {
                        sectionHeader(contacts.count == 2 ? "Alert contacts · 2 of 2" : "Alert contacts · up to 2")
                        Spacer()
                        if contacts.count < 2 {
                            AccountActionChip(symbol: "plus", accessibilityLabel: "Add contact") {
                                let id = -contacts.count - 1
                                contacts.append(AlertContact(id: id, name: "", phone: "", smsConsent: false))
                                // Straight into the new row's name field.
                                Task { @MainActor in
                                    try? await Task.sleep(for: .seconds(0.05))
                                    focusedField = .contactName(id)
                                }
                            }
                        }
                    }
                    VStack(alignment: .leading, spacing: 0) {
                        if contacts.isEmpty {
                            Text("No contacts added — urgent alerts only go to the email/phone on your account.")
                                .font(.cavnarBody(15))
                                .foregroundStyle(Color.cavnarInk3)
                                .padding(.vertical, 9)
                        }
                        ForEach(Array($contacts.enumerated()), id: \.element.id) { index, $contact in
                            HStack(alignment: .center, spacing: 12) {
                                VStack(alignment: .leading, spacing: 6) {
                                    TextField("Name", text: $contact.name)
                                        .font(.cavnarBody(16, weight: 700))
                                        .foregroundStyle(Color.cavnarInk)
                                        .focused($focusedField, equals: .contactName(contact.id))
                                    TextField("Phone", text: $contact.phone)
                                        .font(.cavnarNumber(15))
                                        .foregroundStyle(Color.cavnarInk2)
                                        .keyboardType(.phonePad)
                                        .focused($focusedField, equals: .contactPhone(contact.id))
                                }
                                .id("alerts-contact-\(contact.id)")
                                Spacer(minLength: 8)
                                AccountActionChip(symbol: "xmark", tone: .cavnarRed, accessibilityLabel: "Remove contact") {
                                    contacts.removeAll { $0.id == contact.id }
                                }
                            }
                            .padding(.vertical, 9)
                            if index < contacts.count - 1 { AccountRowDivider() }
                        }
                    }
                    .accountCard()
                }

                if let error = viewModel.saveAlertsError {
                    Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                }

                Button {
                    var toSave = draft
                    toSave.alertQuietStart = quietHoursEnabled ? Self.timeFormatter.string(from: quietStart) : nil
                    toSave.alertQuietEnd = quietHoursEnabled ? Self.timeFormatter.string(from: quietEnd) : nil
                    Task {
                        await viewModel.saveAlertSettings(toSave, contacts: contacts)
                        if viewModel.saveAlertsError == nil {
                            Haptic.success()
                            postedLabel = "Alert settings saved"
                        }
                    }
                } label: {
                    Group {
                        if viewModel.isSavingAlerts {
                            CavnarShimmerText(text: "Saving…")
                        } else {
                            Text("Save alert settings")
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSavingAlerts))
                .disabled(viewModel.isSavingAlerts)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
            // Room for the keyboard under the last contact row — without
            // it the focused field's own scroll-into-view lands the field
            // right at the keyboard's top edge, or under it.
            .padding(.bottom, focusedField == nil ? 0 : 280)
        }
        .scrollDismissesKeyboard(.interactively)
        .onChange(of: focusedField) { _, field in
            guard let field else { return }
            let target: String
            switch field {
            case .extraEmails: target = "alerts-extra-emails"
            case .contactName(let id), .contactPhone(let id): target = "alerts-contact-\(id)"
            }
            // A beat for the keyboard to start rising so the scroll target
            // is measured against the final safe area, not the pre-keyboard one.
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(0.12))
                withAnimation(.easeOut(duration: 0.3)) { proxy.scrollTo(target, anchor: .center) }
            }
        }
        }
        .accountSheetChrome("Alerts")
        .keyboardDoneToolbar { focusedField = nil }
        .cavnarPostedOverlay(postedLabel) { dismiss() }
        .task {
            await PushManager.shared.refreshAuthorization()
            pushDenied = PushManager.shared.authorizationDenied
            await loadBrief()
            await loadNudge()
        }
        }
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
                testPushLabel = "Sent — it should arrive in a second"
            } else {
                testPushLabel = response.error ?? "Apple did not accept it."
            }
        } catch let error as APIClient.APIError {
            testPushLabel = error.message
        } catch {
            testPushLabel = "Couldn't send a test notification."
        }
    }

    // MARK: - What you never open

    struct EngagementSuggestion: Decodable, Identifiable {
        let alertType: String
        let label: String
        let delivered: Int
        let days: Int
        let pushColumn: String

        var id: String { alertType }

        enum CodingKeys: String, CodingKey {
            case label, delivered, days
            case alertType = "alert_type"
            case pushColumn = "push_column"
        }
    }

    private struct EngagementResponse: Decodable {
        let ok: Bool
        let suggestions: [EngagementSuggestion]
    }

    private func loadNudge() async {
        guard let response: EngagementResponse = try? await APIClient.shared.send(
            "/mobile/api/notifications/engagement") else { return }
        nudge = response.ok ? response.suggestions.first : nil
    }

    /// Flips the switch the owner would have flipped themselves, and leaves
    /// the save button to confirm it — nothing here writes on its own.
    private func applyNudge(_ suggestion: EngagementSuggestion) {
        switch suggestion.pushColumn {
        case "al_1star_push": draft.al1starPush = false
        case "al_2star_push": draft.al2starPush = false
        case "al_5star_push": draft.al5starPush = false
        case "al_health_push": draft.alHealthPush = false
        case "al_spike_push": draft.alSpikePush = false
        case "al_unres_push": draft.alUnresPush = false
        default: break
        }
        nudge = nil
        Haptic.light()
    }

    // MARK: - Morning brief & issues

    /// The restaurant's brief settings, from the same /morning-brief
    /// twin the web dashboard reads. Kept separate from `draft` because
    /// they are a different endpoint with a different permission: only a
    /// principal may change them (`canEdit`).
    struct BriefSettings: Decodable {
        var enabled = true
        var hour = 7
        var holdAlerts = true
        var preshiftNudgeHour = 0
        var canEdit = false

        private enum Outer: String, CodingKey { case settings, canEdit = "can_edit" }
        private enum Inner: String, CodingKey {
            case enabled, hour
            case holdAlerts = "hold_alerts"
            case preshiftNudgeHour = "preshift_nudge_hour"
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
        }
    }

    private struct BriefPayload: Encodable {
        let enabled: Bool
        let hour: Int
        let hold_alerts: Bool
        let preshift_nudge_hour: Int
    }

    private static func hourLabel(_ hour: Int) -> String {
        if hour == 0 { return "Off" }
        if hour == 12 { return "12pm" }
        return hour < 12 ? "\(hour)am" : "\(hour - 12)pm"
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

    private func saveBrief() {
        let payload = BriefPayload(enabled: brief.enabled, hour: brief.hour,
                                   hold_alerts: brief.holdAlerts,
                                   preshift_nudge_hour: brief.preshiftNudgeHour)
        Task {
            let _: APIClient.EmptyResponse? = try? await APIClient.shared.send(
                "/mobile/api/morning-brief/settings", method: .post, body: payload)
        }
    }

    // MARK: - Identity (option A)

    private var onCount: Int {
        [draft.alert1star, draft.alert2star, draft.alert5star, draft.alertHealth,
         draft.alertNegSpike, draft.alertNegativeTrend, draft.alertNoResponse, draft.alertLaborOver,
         draft.alertFoodWaste, draft.alertAiVisibilityDrop].filter { $0 }.count
    }

    private var pushCount: Int {
        [draft.al1starPush && draft.alert1star, draft.al2starPush && draft.alert2star,
         draft.al5starPush && draft.alert5star, draft.alHealthPush && draft.alertHealth,
         draft.alSpikePush && draft.alertNegSpike, draft.alUnresPush && draft.alertNoResponse].filter { $0 }.count
    }

    private var hero: some View {
        AccountHero(title: onCount == 0 ? "No alerts on" : "Alerts on") {
            GlowBadge(systemImage: "bell.badge", size: 64)
        } subtitle: {
            Text("\(onCount)").font(.cavnarNumber(15.5, weight: 600))
                + Text(" of ")
                + Text("10").font(.cavnarNumber(15.5, weight: 600))
                + Text(" triggers · Digest \(draft.digestEnabled ? draft.digestDay.capitalized : "off") · Quiet hours \(quietHoursEnabled ? "on" : "off")")
        }
    }

    private var statusStrip: some View {
        HStack(spacing: 8) {
            AccountStatTile(label: "Text", value: draft.urgentViaSms ? "On" : "Off",
                            tone: draft.urgentViaSms ? .cavnarGreen : .cavnarInk3, detail: "Urgent alerts")
            AccountStatTile(label: "Email", value: draft.urgentViaEmail ? "On" : "Off",
                            tone: draft.urgentViaEmail ? .cavnarGreen : .cavnarInk3, detail: "Urgent alerts")
            AccountStatTile(label: "Push", value: "\(pushCount)", detail: "alert types", valueIsNumber: true)
        }
    }

    private func sectionHeader(_ title: String) -> some View {
        AccountKicker(text: title)
    }

    /// A push switch is only meaningful once its alert type is on above —
    /// otherwise it's shown dimmed and inert, not hidden (so the layout
    /// doesn't jump as triggers are toggled).
    private func pushRow(_ label: String, _ binding: Binding<Bool>, on: Bool) -> some View {
        AccountSwitchRow(label: label, isOn: binding, disabled: !on)
            .opacity(on ? 1 : 0.5)
    }

    // One pill that reads the aggregate state of all 8 triggers above it —
    // "Turn on all alerts" while any are off, "Turn off all alerts" once
    // every one already is — rather than two separate buttons.
    private var allAlertsOn: Bool {
        draft.alert1star && draft.alert2star && draft.alert5star && draft.alertHealth
            && draft.alertNegSpike && draft.alertNegativeTrend && draft.alertNoResponse && draft.alertLaborOver
            && draft.alertFoodWaste && draft.alertAiVisibilityDrop
    }

    private func setAllAlerts(_ on: Bool) {
        draft.alert1star = on
        draft.alert2star = on
        draft.alert5star = on
        draft.alertHealth = on
        draft.alertNegSpike = on
        draft.alertNegativeTrend = on
        draft.alertNoResponse = on
        draft.alertLaborOver = on
        draft.alertFoodWaste = on
        draft.alertAiVisibilityDrop = on
    }

    @ViewBuilder
    private var masterAlertPill: some View {
        if allAlertsOn {
            Button {
                withAnimation(.easeOut(duration: 0.2)) { setAllAlerts(false) }
            } label: {
                Text("Turn off all alerts").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
        } else {
            Button {
                withAnimation(.easeOut(duration: 0.2)) { setAllAlerts(true) }
            } label: {
                Text("Turn on all alerts").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
        }
    }
}
