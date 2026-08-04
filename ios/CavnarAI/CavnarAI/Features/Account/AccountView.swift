import SwiftUI

struct AccountView: View {
    @State private var viewModel = AccountViewModel()
    @Environment(SessionStore.self) private var sessionStore

    // NOTE: this screen deliberately avoids Form/Section. As a tab root
    // (its own NavigationStack, not a pushed view), a bare Form here never
    // delivered a single tap to any Button/Toggle inside it — confirmed with
    // a trivial one-line reproduction — while every other tab root in the
    // app (Home, Modules) uses ScrollView/VStack/LazyVGrid and works fine.
    // Rather than chase the underlying SwiftUI/UIKit interaction further,
    // this follows the same proven ScrollView+VStack pattern as everywhere
    // else, with plain VStack "cards" standing in for Form sections.
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let summary = viewModel.summary {
                        content(summary)
                    } else if viewModel.isLoading {
                        ProgressView().padding(.top, 60)
                    } else if let error = viewModel.errorMessage {
                        VStack(spacing: 8) {
                            Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                            Button("Retry") { Task { await viewModel.load() } }
                        }
                        .padding(.top, 60)
                        .frame(maxWidth: .infinity)
                    }
                }
                .padding(20)
            }
            .background(Color.cavnarPaper)
            .refreshable { await viewModel.load() }
            .navigationTitle("Account")
            .task {
                await viewModel.load()
                await viewModel.loadBilling()
            }
        }
    }

    @ViewBuilder
    private func content(_ summary: AccountSummary) -> some View {
        profileSection(summary.profile)
        securitySection(summary.account)
        sessionsSection
        connectionsSection(summary.connections)
        alertsSection(summary.alerts)
        billingSection
        signOutSection
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.cavnarBody(11, weight: 700))
            .foregroundStyle(Color.cavnarInk3)
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
            Spacer()
            Text(value).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
        }
    }

    // MARK: - Profile (read-only, matches the web Account tab's profile card)

    @ViewBuilder
    private func profileSection(_ profile: AccountProfile) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("Restaurant Profile")
            VStack(alignment: .leading, spacing: 10) {
                row("Restaurant", profile.restaurantName)
                if let location = profile.locationName {
                    row("Location", location)
                }
                row("Owner", profile.ownerName ?? "—")
                row("Email", profile.ownerEmail ?? "—")
                row("Phone", profile.ownerPhone ?? "—")
                if let neighborhood = profile.neighborhood {
                    row("Neighborhood", neighborhood)
                }
                if let vibe = profile.vibe {
                    row("Atmosphere", vibe)
                }
                Text("To update your profile details, email will@cavnar.ai")
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk3)
                    .padding(.top, 4)
            }
            .cavnarCard()
        }
    }

    // MARK: - Security

    @State private var showingChangePassword = false
    @State private var showing2FASetup = false

    @ViewBuilder
    private func securitySection(_ account: AccountInfo) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("Security")
            VStack(alignment: .leading, spacing: 12) {
                row("Username", account.username)
                row("Email", account.email)

                if account.twoFAEnabled {
                    HStack {
                        Text("Two-factor authentication").font(.cavnarBody(13))
                        Spacer()
                        Text("On").font(.cavnarBody(12, weight: 700)).foregroundStyle(Color.cavnarGreen)
                    }
                    Button("Disable two-factor authentication", role: .destructive) {
                        Task { await viewModel.disable2FA() }
                    }
                    .font(.cavnarBody(13, weight: 600))
                } else {
                    Button("Enable two-factor authentication") {
                        showing2FASetup = true
                    }
                    .font(.cavnarBody(13, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
                }

                Toggle(isOn: Binding(
                    get: { account.loginNotify },
                    set: { newValue in Task { await viewModel.toggleLoginNotify(newValue) } }
                )) {
                    Text("Sign-in notifications").font(.cavnarBody(13))
                }
                .tint(Color.cavnarEmber)

                Button("Change password") { showingChangePassword = true }
                    .font(.cavnarBody(13, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
            }
            .cavnarCard()
        }
        .sheet(isPresented: $showingChangePassword) {
            ChangePasswordSheet(viewModel: viewModel)
        }
        .sheet(isPresented: $showing2FASetup) {
            TwoFactorSetupSheet(viewModel: viewModel)
        }
    }

    // MARK: - Active sessions

    @ViewBuilder
    private var sessionsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("Active Sessions")
            VStack(alignment: .leading, spacing: 12) {
                ForEach(viewModel.sessions) { session in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(session.label).font(.cavnarBody(13, weight: 600))
                            if session.isCurrent {
                                Text("This device")
                                    .font(.cavnarBody(10, weight: 700))
                                    .foregroundStyle(Color.cavnarEmber)
                            }
                        }
                        Text("Last active \(session.lastActive)")
                            .font(.cavnarBody(11))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                Button("Sign out all other devices", role: .destructive) {
                    Task { await viewModel.revokeOtherSessions() }
                }
                .font(.cavnarBody(13, weight: 600))
            }
            .cavnarCard()
        }
    }

    // MARK: - Connections (read-only status — connecting a POS/social/GBP
    // account is an OAuth redirect flow that stays a desktop action for now;
    // see mobile_api.py's Account/Settings section docstring.)

    @ViewBuilder
    private func connectionsSection(_ connections: AccountConnections) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("Connections")
            VStack(alignment: .leading, spacing: 12) {
                connectionRow("Google Business", connections.googleBusiness)
                connectionRow("Instagram & Facebook", connections.instagram)
                connectionRow("Toast POS", connections.toast)
                connectionRow("Square POS", connections.square)
                connectionRow("Clover POS", connections.clover)
            }
            .cavnarCard()
        }
    }

    @ViewBuilder
    private func connectionRow(_ label: String, _ status: ConnectionStatus) -> some View {
        HStack {
            Text(label).font(.cavnarBody(13, weight: 600))
            Spacer()
            Text(status.connected ? "Connected" : "Not connected")
                .font(.cavnarBody(12))
                .foregroundStyle(status.connected ? Color.cavnarGreen : Color.cavnarInk3)
        }
    }

    // MARK: - Alerts

    @State private var alertDraft: AlertSettings?

    @ViewBuilder
    private func alertsSection(_ alerts: AccountAlerts) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("Alerts")
            VStack(alignment: .leading, spacing: 12) {
                let settings = alertDraft ?? alerts.settings

                alertToggle("1-star reviews", bindingFor(\.alert1star, alerts))
                alertToggle("2-star reviews", bindingFor(\.alert2star, alerts))
                alertToggle("5-star reviews", bindingFor(\.alert5star, alerts))
                alertToggle("Negative review spike", bindingFor(\.alertNegSpike, alerts))
                alertToggle("Labor over target", bindingFor(\.alertLaborOver, alerts))
                alertToggle("Text alerts", bindingFor(\.urgentViaSms, alerts))
                alertToggle("Weekly digest", bindingFor(\.digestEnabled, alerts))

                ForEach(alerts.contacts) { contact in
                    VStack(alignment: .leading) {
                        Text(contact.name.isEmpty ? "Contact" : contact.name).font(.cavnarBody(13, weight: 600))
                        Text(contact.phone).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                    }
                }

                if let error = viewModel.saveAlertsError {
                    Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                }
                Button("Save alert settings") {
                    Task {
                        await viewModel.saveAlertSettings(settings, contacts: alerts.contacts)
                        alertDraft = nil
                    }
                }
                .font(.cavnarBody(13, weight: 600))
                .foregroundStyle(Color.cavnarEmber)
                .disabled(viewModel.isSavingAlerts)
            }
            .cavnarCard()
        }
    }

    private func alertToggle(_ label: String, _ binding: Binding<Bool>) -> some View {
        Toggle(isOn: binding) {
            Text(label).font(.cavnarBody(13))
        }
        .tint(Color.cavnarEmber)
    }

    private func bindingFor(_ keyPath: WritableKeyPath<AlertSettings, Bool>, _ alerts: AccountAlerts) -> Binding<Bool> {
        Binding(
            get: { (alertDraft ?? alerts.settings)[keyPath: keyPath] },
            set: { newValue in
                var draft = alertDraft ?? alerts.settings
                draft[keyPath: keyPath] = newValue
                alertDraft = draft
            }
        )
    }

    // MARK: - Billing

    @ViewBuilder
    private var billingSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("Billing")
            VStack(alignment: .leading, spacing: 10) {
                if let billing = viewModel.billing {
                    if billing.ok, billing.status != "inactive" {
                        row("Next charge", billing.nextDate ?? "—")
                        row("Amount", billing.amount ?? "—")
                        row("Payment method", billing.paymentMethod ?? "—")
                        if let urlString = billing.portalURL, let url = URL(string: urlString) {
                            Link("Manage payment method", destination: url)
                                .font(.cavnarBody(13, weight: 600))
                        }
                    } else {
                        Text(billing.message ?? "No active subscription. Contact will@cavnar.ai")
                            .font(.cavnarBody(12))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                } else {
                    ProgressView()
                }
            }
            .cavnarCard()
        }
    }

    // MARK: - Sign out

    @ViewBuilder
    private var signOutSection: some View {
        Button("Sign Out", role: .destructive) {
            Task { await sessionStore.logout() }
        }
        .font(.cavnarBody(14, weight: 700))
        .frame(maxWidth: .infinity)
        .padding(.vertical, 12)
    }
}

private struct ChangePasswordSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var current = ""
    @State private var newPassword = ""

    var body: some View {
        NavigationStack {
            Form {
                SecureField("Current password", text: $current)
                SecureField("New password", text: $newPassword)
                if let error = viewModel.changePasswordError {
                    Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                }
                Button("Change password") {
                    Task {
                        await viewModel.changePassword(current: current, newPassword: newPassword)
                        if viewModel.changePasswordSucceeded { dismiss() }
                    }
                }
                .disabled(viewModel.isChangingPassword || current.isEmpty || newPassword.count < 8)
            }
            .navigationTitle("Change Password")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}

private struct TwoFactorSetupSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var code = ""

    var body: some View {
        NavigationStack {
            Form {
                if let masked = viewModel.twoFATestMasked {
                    Text("Code sent to \(masked)").font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                    TextField("6-digit code", text: $code)
                        .keyboardType(.numberPad)
                    Button("Verify and enable") {
                        Task {
                            if await viewModel.verify2FA(code: code) { dismiss() }
                        }
                    }
                    .disabled(viewModel.is2FABusy || code.count != 6)
                } else {
                    Button("Send test code") {
                        Task { await viewModel.send2FATest() }
                    }
                    .disabled(viewModel.is2FABusy)
                }
                if let error = viewModel.twoFAError {
                    Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                }
            }
            .navigationTitle("Enable 2FA")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}
