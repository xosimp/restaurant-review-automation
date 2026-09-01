import SwiftUI

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
                            Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                            Button("Retry") { Task { await viewModel.load() } }
                        }
                        .padding(.top, 60)
                        .frame(maxWidth: .infinity)
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
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
                    .font(.cavnarBody(12))
                    .foregroundStyle(Color.cavnarInk3)
                    .lineLimit(1)
            }

            Spacer(minLength: 8)

            if let billing = viewModel.billing, billing.ok, let status = billing.status {
                let isActive = status == "active" || status == "trialing"
                HStack(spacing: 5) {
                    Circle().fill(isActive ? Color.cavnarGreen : Color.cavnarAmber).frame(width: 6, height: 6)
                    Text(status.uppercased())
                        .font(.cavnarBody(10, weight: 700))
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
    // Same key RootView reads for .preferredColorScheme — this row is the
    // only place that ever writes it.
    @AppStorage("cavnarLightMode") private var isLightMode = false

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
                        trailing: viewModel.sessions.isEmpty ? nil : "\(viewModel.sessions.count)"
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
                lightModeRow
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

    // Black-with-cream-seal vs. white-with-black-seal — every color token
    // already ships both (see RootView's isLightMode comment); this is the
    // only place that flips which one is live. No manual Haptic.selection()
    // — Toggle/UISwitch already fires its own.
    private var lightModeRow: some View {
        HStack(spacing: 13) {
            Image(systemName: "circle.lefthalf.filled")
                .font(.system(size: 15))
                .foregroundStyle(Color.cavnarInk3)
                .frame(width: 18)
            Text("Light appearance")
                .font(.cavnarBody(15.5, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Spacer()
            Toggle("", isOn: $isLightMode)
                .labelsHidden()
                .tint(Color.cavnarEmber)
        }
        .padding(.horizontal, 16)
        .frame(height: 54)
    }

    private func group<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title.uppercased())
                .font(.cavnarBody(11.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 0) { content() }
                .background(Color.cavnarPaper2)
                .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: 16))
        }
    }

    private func row(_ label: String, systemImage: String, trailing: String? = nil) -> some View {
        HStack(spacing: 13) {
            Image(systemName: systemImage)
                .font(.system(size: 15))
                .foregroundStyle(Color.cavnarInk3)
                .frame(width: 18)
            Text(label)
                .font(.cavnarBody(15.5, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Spacer()
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
        .font(.cavnarBody(14, weight: 700))
        .foregroundStyle(Color.cavnarRed)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 13)
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.cavnarRed.opacity(0.35), lineWidth: 1))
        .padding(.top, 4)
    }
}
