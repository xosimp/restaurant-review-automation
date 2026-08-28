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

    @State private var draft: AlertSettings
    @State private var contacts: [AlertContact]
    @State private var quietHoursEnabled: Bool
    @State private var quietStart: Date
    @State private var quietEnd: Date

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
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 4) {
                    sectionHeader("What triggers an alert")
                    VStack(alignment: .leading, spacing: 12) {
                        toggle("1-star reviews", $draft.alert1star)
                        toggle("2-star reviews", $draft.alert2star)
                        toggle("5-star reviews", $draft.alert5star)
                        toggle("Health or safety mention", $draft.alertHealth)
                        toggle("Negative review spike", $draft.alertNegSpike)
                        toggle("Rating declining trend", $draft.alertNegativeTrend)
                        toggle("Unresponded review (48h)", $draft.alertNoResponse)
                        toggle("Labor over target", $draft.alertLaborOver)
                    }
                    .cavnarCard()
                }

                VStack(alignment: .leading, spacing: 4) {
                    sectionHeader("How urgent alerts reach you")
                    VStack(alignment: .leading, spacing: 12) {
                        toggle("Text alerts", $draft.urgentViaSms)
                        toggle("Email alerts", $draft.urgentViaEmail)
                    }
                    .cavnarCard()
                }

                VStack(alignment: .leading, spacing: 4) {
                    sectionHeader("Quiet hours")
                    VStack(alignment: .leading, spacing: 12) {
                        Toggle(isOn: $quietHoursEnabled) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Pause overnight").font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                                Text("Urgent alerts wait until your quiet window ends").font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
                            }
                        }
                        .tint(Color.cavnarEmber)
                        if quietHoursEnabled {
                            DatePicker("From", selection: $quietStart, displayedComponents: .hourAndMinute)
                                .font(.cavnarBody(13))
                                .tint(Color.cavnarEmber)
                            DatePicker("Until", selection: $quietEnd, displayedComponents: .hourAndMinute)
                                .font(.cavnarBody(13))
                                .tint(Color.cavnarEmber)
                        }
                    }
                    .cavnarCard()
                }

                VStack(alignment: .leading, spacing: 4) {
                    sectionHeader("Weekly digest")
                    VStack(alignment: .leading, spacing: 12) {
                        toggle("Weekly digest", $draft.digestEnabled)
                        if draft.digestEnabled {
                            HStack {
                                Text("Delivered on").font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                                Spacer()
                                Picker("", selection: $draft.digestDay) {
                                    ForEach(Self.days, id: \.self) { day in
                                        Text(day.capitalized).tag(day)
                                    }
                                }
                                .tint(Color.cavnarEmber)
                            }
                        }
                    }
                    .cavnarCard()
                }

                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        sectionHeader("Alert contacts")
                        Spacer()
                        if contacts.count < 2 {
                            Button {
                                Haptic.light()
                                contacts.append(AlertContact(id: -contacts.count - 1, name: "", phone: "", smsConsent: false))
                            } label: {
                                Text("+ Add").font(.cavnarBody(11, weight: 700)).foregroundStyle(Color.cavnarEmber)
                            }
                        }
                    }
                    VStack(alignment: .leading, spacing: 16) {
                        if contacts.isEmpty {
                            Text("No contacts added — urgent alerts only go to the email/phone on your account.")
                                .font(.cavnarBody(12))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        ForEach($contacts) { $contact in
                            HStack(alignment: .top, spacing: 10) {
                                VStack(spacing: 8) {
                                    TextField("Name", text: $contact.name)
                                        .font(.cavnarBody(13, weight: 600))
                                        .foregroundStyle(Color.cavnarInk)
                                    Rectangle().fill(Color.cavnarPaper3).frame(height: 1)
                                    TextField("Phone", text: $contact.phone)
                                        .font(.cavnarBody(13))
                                        .foregroundStyle(Color.cavnarInk)
                                        .keyboardType(.phonePad)
                                }
                                Button {
                                    Haptic.light()
                                    contacts.removeAll { $0.id == contact.id }
                                } label: {
                                    Image(systemName: "xmark.circle.fill")
                                        .font(.system(size: 15))
                                        .foregroundStyle(Color.cavnarInk3)
                                }
                                .padding(.top, 4)
                            }
                        }
                    }
                    .cavnarCard()
                }

                if let error = viewModel.saveAlertsError {
                    Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                }

                Button {
                    var toSave = draft
                    toSave.alertQuietStart = quietHoursEnabled ? Self.timeFormatter.string(from: quietStart) : nil
                    toSave.alertQuietEnd = quietHoursEnabled ? Self.timeFormatter.string(from: quietEnd) : nil
                    Task {
                        await viewModel.saveAlertSettings(toSave, contacts: contacts)
                        if viewModel.saveAlertsError == nil { dismiss() }
                    }
                } label: {
                    if viewModel.isSavingAlerts {
                        ProgressView().tint(.white)
                    } else {
                        Text("Save alert settings")
                    }
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSavingAlerts))
                .disabled(viewModel.isSavingAlerts)
                .frame(maxWidth: .infinity)
            }
            .padding(20)
        }
        .cavnarModuleBackground()
        .navigationTitle("Alerts")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.cavnarBody(11, weight: 700))
            .foregroundStyle(Color.cavnarInk3)
    }

    private func toggle(_ label: String, _ binding: Binding<Bool>) -> some View {
        Toggle(isOn: binding) {
            Text(label).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk)
        }
        .tint(Color.cavnarEmber)
    }
}
