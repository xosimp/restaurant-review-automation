import SwiftUI
import UIKit

/// Rebuilt around Option A from the Account design review — a hero
/// identity block, then settings collapsed into labelled groups (was 9
/// flat, visually-identical cards), each row opening its own detail sheet
/// rather than every setting living inline on one long scroll.
struct AccountView: View {
    @State private var viewModel = AccountViewModel()
    @Environment(SessionStore.self) private var sessionStore

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let summary = viewModel.summary {
                        content(summary)
                    } else if viewModel.isLoading {
                        CavnarLoadingSeal()
                            .padding(.top, 60)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else if let error = viewModel.errorMessage {
                        VStack(spacing: 8) {
                            Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
                            Button("Retry") { Task { await viewModel.load() } }
                        }
                        .padding(.top, 60)
                        .frame(maxWidth: .infinity)
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .cavnarEmberRefreshable { await viewModel.load() }
            .navigationTitle("Account")
            // Inline only — the centered Clash Display title below is the
            // one that's drawn; the system's large top-left title doubled it.
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Account") }
            .task {
                await viewModel.load()
                await viewModel.loadBilling()
                // Resume an in-progress 2FA setup that a Face ID relock
                // (e.g. backgrounding to read the emailed code) tore down —
                // see SessionStore.pendingTwoFactorSetupEmail. Gated on
                // load() finishing since the Security sheet needs
                // summary.account, which only exists once that's loaded.
                if sessionStore.pendingTwoFactorSetupEmail != nil {
                    showingSecurity = true
                }
            }
        }
    }

    @ViewBuilder
    private func content(_ summary: AccountSummary) -> some View {
        heroIdentity(summary)
        groupedSettings(summary)
        signOutSection
    }

    // MARK: - Hero identity

    private func initials(_ name: String) -> String {
        let words = name.split(separator: " ")
        let letters = words.prefix(2).compactMap { $0.first }
        return String(letters).uppercased()
    }

    private func heroIdentity(_ summary: AccountSummary) -> some View {
        HStack(spacing: 14) {
            Text(initials(summary.profile.restaurantName))
                .font(.cavnarHeadline(21))
                .foregroundStyle(Color.cavnarEmber2)
                .frame(width: 54, height: 54)
                .background(Color.cavnarEmber.opacity(0.16))
                .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.cavnarEmber.opacity(0.45), lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: 16))

            VStack(alignment: .leading, spacing: 3) {
                Text(summary.profile.restaurantName)
                    .font(.cavnarBody(17, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Text("\(summary.account.email) · \(sessionStore.currentUser?.isOwner == true ? "Owner" : "Manager")")
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .lineLimit(1)
            }

            Spacer(minLength: 8)

            if let billing = viewModel.billing, billing.ok, let status = billing.status {
                let isActive = status == "active" || status == "trialing"
                HStack(spacing: 5) {
                    Circle().fill(isActive ? Color.cavnarGreen : Color.cavnarAmber).frame(width: 6, height: 6)
                    Text(status.uppercased())
                        .font(.cavnarBody(15, weight: 700))
                        .foregroundStyle(isActive ? Color.cavnarGreen : Color.cavnarAmber)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .background((isActive ? Color.cavnarGreen : Color.cavnarAmber).opacity(0.14))
                .clipShape(Capsule())
            }
        }
        .padding(.bottom, 6)
    }

    // MARK: - Grouped settings

    @State private var showingProfile = false
    @State private var showingSecurity = false
    @State private var showingAlerts = false
    @State private var showingConnections = false
    @State private var showingBilling = false
    @State private var showingScheduleHistory = false
    @State private var showingChangelog = false
    @State private var changelogBadge = ChangelogBadgeViewModel()
    // Reflects the actual system state (UIApplication.shared.alternateIconName),
    // not a preference of our own — this is the home-screen icon, which iOS
    // owns, not the in-app interface (that stays dark-only, see RootView).
    @State private var isLightAppIcon = UIApplication.shared.alternateIconName == "AppIconLight"
    @State private var appIconError: String?

    private func groupedSettings(_ summary: AccountSummary) -> some View {
        VStack(alignment: .leading, spacing: 24) {
            group("Restaurant") {
                settingsRow {
                    row("Profile & details", systemImage: "building.2")
                } action: {
                    showingProfile = true
                }
            }
            .sheet(isPresented: $showingProfile) {
                AccountProfileDetailView(viewModel: viewModel, profile: summary.profile)
            }

            group("Security") {
                settingsRow {
                    row(
                        "Security & devices", systemImage: "lock.shield",
                        trailing: viewModel.sessions.isEmpty ? nil : "\(viewModel.sessions.count)",
                        badge: summary.account.twoFAEnabled ? "2FA ON" : nil
                    )
                } action: {
                    showingSecurity = true
                }
            }
            .sheet(isPresented: $showingSecurity) {
                AccountSecurityDetailView(viewModel: viewModel, account: summary.account)
            }

            group("Alerts") {
                settingsRow {
                    let onCount = [
                        summary.alerts.settings.alert1star, summary.alerts.settings.alert2star,
                        summary.alerts.settings.alert5star, summary.alerts.settings.alertHealth,
                        summary.alerts.settings.alertNegSpike, summary.alerts.settings.alertNegativeTrend,
                        summary.alerts.settings.alertNoResponse, summary.alerts.settings.alertLaborOver,
                    ].filter { $0 }.count
                    row("Alerts & digest", systemImage: "bell", trailing: "\(onCount) on")
                } action: {
                    showingAlerts = true
                }
            }
            .sheet(isPresented: $showingAlerts) {
                AccountAlertsDetailView(viewModel: viewModel, alerts: summary.alerts)
            }

            group("Connections") {
                settingsRow {
                    let connectedCount = [
                        summary.connections.googleBusiness, summary.connections.instagram,
                        summary.connections.toast, summary.connections.square, summary.connections.clover,
                    ].filter(\.connected).count
                    row("Connected apps", systemImage: "link", trailing: "\(connectedCount) of 5")
                } action: {
                    showingConnections = true
                }
            }
            .sheet(isPresented: $showingConnections) {
                AccountConnectionsDetailView(viewModel: viewModel, connections: summary.connections)
            }

            group("Billing") {
                settingsRow {
                    row("Plan & payment", systemImage: "creditcard", trailing: viewModel.billing?.amount)
                } action: {
                    showingBilling = true
                }
            }
            .sheet(isPresented: $showingBilling) {
                AccountBillingDetailView(billing: viewModel.billing)
            }

            group("More") {
                settingsRow {
                    row("Schedule History", systemImage: "clock.arrow.circlepath")
                } action: {
                    showingScheduleHistory = true
                }
                Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1).padding(.leading, 47)
                settingsRow {
                    row(
                        "What's New", systemImage: "sparkles",
                        trailing: changelogBadge.unreadCount > 0 ? "\(changelogBadge.unreadCount)" : nil
                    )
                } action: {
                    showingChangelog = true
                }
                Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1).padding(.leading, 47)
                appIconRow
            }
            .task { await changelogBadge.refresh() }
            .sheet(isPresented: $showingScheduleHistory) {
                ScheduleHistoryView()
            }
            .sheet(isPresented: $showingChangelog) {
                ChangelogView()
            }
        }
    }

    private func settingsRow<Content: View>(@ViewBuilder _ label: () -> Content, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            label()
        }
        .foregroundStyle(Color.cavnarInk)
    }

    // The HOME-SCREEN icon only — black-with-cream-seal (default) vs.
    // white-with-black-seal (AppIconLight, registered in project.yml's
    // CFBundleAlternateIcons). The in-app interface stays dark-only
    // regardless (see RootView) — this is purely UIApplication's own
    // alternate-icon mechanism, not our own preference storage, so the
    // toggle always reflects whatever's actually active rather than
    // trusting a stale local flag if the switch silently failed.
    private var appIconRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 13) {
                Image(systemName: "circle.lefthalf.filled")
                    .font(.system(size: 15))
                    .foregroundStyle(Color.cavnarInk3)
                    .frame(width: 18)
                Text("Light app icon")
                    .font(.cavnarBody(15.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                Toggle("", isOn: Binding(
                    get: { isLightAppIcon },
                    set: { newValue in setAppIcon(light: newValue) }
                ))
                .labelsHidden()
                .tint(Color.cavnarEmber)
            }
            .padding(.horizontal, 16)
            .frame(height: 54)
            if let appIconError {
                Text(appIconError)
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarRed)
                    .padding(.horizontal, 16)
                    .padding(.bottom, 10)
            }
        }
    }

    private func setAppIcon(light: Bool) {
        guard UIApplication.shared.supportsAlternateIcons else {
            appIconError = "This device doesn't support switching app icons."
            return
        }
        Haptic.light()
        appIconError = nil
        let targetName = light ? "AppIconLight" : nil
        UIApplication.shared.setAlternateIconName(targetName) { error in
            Task { @MainActor in
                if let error {
                    appIconError = error.localizedDescription
                    // Reflect whatever's actually active, not the tap's
                    // intent — a failed switch means the toggle should
                    // snap back rather than show a state that didn't apply.
                    isLightAppIcon = UIApplication.shared.alternateIconName == "AppIconLight"
                } else {
                    isLightAppIcon = light
                }
            }
        }
    }

    private func group<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title.uppercased())
                .font(.cavnarBody(15.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 0) { content() }
                .background(Color.cavnarPaper2)
                .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: 16))
        }
    }

    private func row(_ label: String, systemImage: String, trailing: String? = nil, badge: String? = nil) -> some View {
        HStack(spacing: 13) {
            Image(systemName: systemImage)
                .font(.system(size: 15))
                .foregroundStyle(Color.cavnarInk3)
                .frame(width: 18)
            Text(label)
                .font(.cavnarBody(15.5, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Spacer()
            // A status pill (2FA on/off) is a different kind of signal
            // than the plain gray count `trailing` shows elsewhere — its
            // own tinted capsule so it reads at a glance, not just more
            // gray text easy to skim past.
            if let badge {
                HStack(spacing: 4) {
                    Circle().fill(Color.cavnarGreen).frame(width: 6, height: 6)
                    Text(badge)
                        .font(.cavnarBody(13.5, weight: 700))
                        .foregroundStyle(Color.cavnarGreen)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color.cavnarGreen.opacity(0.14))
                .clipShape(Capsule())
            }
            if let trailing {
                Text(trailing)
                    .font(.cavnarNumber(14, weight: 600))
                    .foregroundStyle(Color.cavnarInk3)
            }
            Image(systemName: "chevron.right")
                .font(.system(size: 12))
                .foregroundStyle(Color.cavnarInk3)
        }
        .padding(.horizontal, 16)
        .frame(height: 54)
        .contentShape(Rectangle())
    }

    // MARK: - Sign out

    @ViewBuilder
    private var signOutSection: some View {
        Button("Sign Out", role: .destructive) {
            Haptic.light()
            Task { await sessionStore.logout() }
        }
        .font(.cavnarBody(15, weight: 700))
        .foregroundStyle(Color.cavnarRed)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 13)
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.cavnarRed.opacity(0.35), lineWidth: 1))
        .padding(.top, 4)
    }
}
