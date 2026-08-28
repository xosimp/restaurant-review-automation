import SwiftUI

/// Rebuilt around Option A from the Account design review — a hero
/// identity block, then settings collapsed into 5 labelled groups (was 9
/// flat, visually-identical cards), each row pushing to its own detail
/// screen rather than every setting living inline on one long scroll.
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
                        ProgressView()
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
        scheduleHistorySection
        changelogSection
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
                .font(.cavnarHeadline(21, weight: .semiBold))
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

    private func groupedSettings(_ summary: AccountSummary) -> some View {
        VStack(alignment: .leading, spacing: 24) {
            group("Restaurant") {
                NavigationLink {
                    AccountProfileDetailView(viewModel: viewModel, profile: summary.profile)
                } label: {
                    row("Profile & details", systemImage: "building.2")
                }
            }

            group("Security") {
                NavigationLink {
                    AccountSecurityDetailView(viewModel: viewModel, account: summary.account)
                } label: {
                    row(
                        "Security & devices", systemImage: "lock.shield",
                        trailing: viewModel.sessions.isEmpty ? nil : "\(viewModel.sessions.count)"
                    )
                }
            }

            group("Alerts") {
                NavigationLink {
                    AccountAlertsDetailView(viewModel: viewModel, alerts: summary.alerts)
                } label: {
                    let onCount = [
                        summary.alerts.settings.alert1star, summary.alerts.settings.alert2star,
                        summary.alerts.settings.alert5star, summary.alerts.settings.alertHealth,
                        summary.alerts.settings.alertNegSpike, summary.alerts.settings.alertNegativeTrend,
                        summary.alerts.settings.alertNoResponse, summary.alerts.settings.alertLaborOver,
                    ].filter { $0 }.count
                    row("Alerts & digest", systemImage: "bell", trailing: "\(onCount) on")
                }
            }

            group("Connections") {
                NavigationLink {
                    AccountConnectionsDetailView(connections: summary.connections)
                } label: {
                    let connectedCount = [
                        summary.connections.googleBusiness, summary.connections.instagram,
                        summary.connections.toast, summary.connections.square, summary.connections.clover,
                    ].filter(\.connected).count
                    row("Connected apps", systemImage: "link", trailing: "\(connectedCount) of 5")
                }
            }

            group("Billing") {
                NavigationLink {
                    AccountBillingDetailView(billing: viewModel.billing)
                } label: {
                    row("Plan & payment", systemImage: "creditcard", trailing: viewModel.billing?.amount)
                }
            }
        }
    }

    private func group<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title.uppercased())
                .font(.cavnarBody(10, weight: 700))
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
                .font(.cavnarBody(14, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Spacer()
            if let trailing {
                Text(trailing)
                    .font(.cavnarNumber(13, weight: 600))
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

    // MARK: - Schedule History

    // A durable server-side record of every generated schedule, reachable
    // here independent of the Labor tab's own client-side caching — see
    // ScheduleHistoryView's doc comment. Kept as a sheet, not folded into
    // the grouped NavigationLink list above — it's its own whole flow
    // (like What's New below it), not a settings group.
    @State private var showingScheduleHistory = false

    @ViewBuilder
    private var scheduleHistorySection: some View {
        Button {
            Haptic.light()
            showingScheduleHistory = true
        } label: {
            row("Schedule History", systemImage: "clock.arrow.circlepath")
        }
        .foregroundStyle(Color.cavnarInk)
        .background(Color.cavnarPaper2)
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .sheet(isPresented: $showingScheduleHistory) {
            ScheduleHistoryView()
        }
    }

    // MARK: - Changelog

    @State private var changelogBadge = ChangelogBadgeViewModel()
    @State private var showingChangelog = false

    @ViewBuilder
    private var changelogSection: some View {
        Button {
            Haptic.light()
            showingChangelog = true
        } label: {
            row(
                "What's New", systemImage: "sparkles",
                trailing: changelogBadge.unreadCount > 0 ? "\(changelogBadge.unreadCount)" : nil
            )
        }
        .foregroundStyle(Color.cavnarInk)
        .background(Color.cavnarPaper2)
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .task { await changelogBadge.refresh() }
        .sheet(isPresented: $showingChangelog) {
            ChangelogView()
        }
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
