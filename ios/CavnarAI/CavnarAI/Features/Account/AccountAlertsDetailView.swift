import SwiftUI

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
                        Text("Alert and digest emails also go to these addresses.")
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3.opacity(0.8))
                    }
                    .padding(.vertical, 9)
                }

                AccountSection(kicker: "Push notifications") {
                    pushRow("1-star reviews", $draft.al1starPush, on: draft.alert1star)
                    pushRow("2-star reviews", $draft.al2starPush, on: draft.alert2star)
                    pushRow("5-star reviews", $draft.al5starPush, on: draft.alert5star)
                    pushRow("Health or safety mention", $draft.alHealthPush, on: draft.alertHealth)
                    pushRow("Negative review spike", $draft.alSpikePush, on: draft.alertNegSpike)
                    pushRow("Unresponded review (48h)", $draft.alUnresPush, on: draft.alertNoResponse)
                    AccountSwitchRow(label: "Play a sound", isOn: $draft.pushSound)
                    Text("Push doesn't need text/email alerts turned on — it's free to send, so it's gated per-alert-type here instead. Trend, labor, waste and visibility alerts push automatically once enabled.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3.opacity(0.8))
                        .padding(.vertical, 9)
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
                                contacts.append(AlertContact(id: -contacts.count - 1, name: "", phone: "", smsConsent: false))
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
                                    TextField("Phone", text: $contact.phone)
                                        .font(.cavnarNumber(15))
                                        .foregroundStyle(Color.cavnarInk2)
                                        .keyboardType(.phonePad)
                                }
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
        }
        .accountSheetChrome("Alerts")
        .cavnarPostedOverlay(postedLabel) { dismiss() }
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
