import StoreKit
import SwiftUI
import UIKit

/// Rebuilt around Option A from the Account design review — a hero
/// identity block, then settings collapsed into labelled groups (was 9
/// flat, visually-identical cards), each row opening its own detail sheet
/// rather than every setting living inline on one long scroll.
struct AccountView: View {
    @State private var viewModel = AccountViewModel()
    @Environment(SessionStore.self) private var sessionStore
    @Environment(DeepLinkRouter.self) private var deepLinkRouter
    @Environment(\.openURL) private var openURL

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let summary = viewModel.summary {
                        content(summary)
                    } else if viewModel.isLoading {
                        // A centered seal has no relationship to the real
                        // layout, so content landing shoved the whole page
                        // around — on a slow connection that window is long
                        // enough to cause genuine mis-taps (audit 7.8). This
                        // reserves the hero + group geometry instead, matching
                        // HomeView's own heroSkeleton approach.
                        loadingSkeleton
                    } else if let error = viewModel.errorMessage {
                        VStack(spacing: 8) {
                            Text(error).cavnarText(.body)
                            Button("Retry") { Task { await viewModel.load() } }
                            // Account is the only screen that can sign someone
                            // out, and its own content — Sign Out included —
                            // only renders once this same load succeeds. A
                            // session the server will never accept (wrong
                            // environment, revoked, anything) used to trap
                            // the user here permanently: every retry fails
                            // the same way, and there was no way out short of
                            // knowing to delete the app. This is that way out.
                            Button("Sign Out", role: .destructive) {
                                Task { await sessionStore.logout() }
                            }
                            .padding(.top, 4)
                        }
                        .padding(.top, 60)
                        .frame(maxWidth: .infinity)
                    }
                }
                .padding(20)
                // Account's groups read as one column on an iPad (#99).
                .cavnarReadableWidth()
                .animation(.easeOut(duration: 0.25), value: viewModel.summary == nil)
            }
            .cavnarModuleBackground()
            .cavnarEmberRefreshable {
                await viewModel.load()
                await viewModel.loadHealth()
            }
            .navigationTitle("Account")
            // Inline only — the centered Clash Display title below is the
            // one that's drawn; the system's large top-left title doubled it.
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Account") }
            // account/<section> from a notification, the command sheet or a
            // link: that section's sheet, once Account has loaded (the
            // sheets read its summary) — F3-15.
            .onChange(of: deepLinkRouter.pendingAccountSection) { _, _ in openLinkedSection() }
            .onChange(of: viewModel.summary != nil) { _, _ in openLinkedSection() }
            // A sheet closing may have fixed a health item: read it again.
            .onChange(of: anySheetOpen) { _, open in
                if !open { Task { await viewModel.loadHealth() } }
            }
            .sheet(item: $webTarget) { target in
                CavnarSafariView(url: target.url).ignoresSafeArea()
            }
            .task {
                await viewModel.load()
                await viewModel.loadHealth()
                // Billing is the account owner's (403 owner_only otherwise).
                if isOwner { await viewModel.loadBilling() }
                // Resume an in-progress 2FA setup that a Face ID relock
                // (e.g. backgrounding to read the emailed code) tore down —
                // see SessionStore.pendingTwoFactorSetupEmail. Gated on
                // load() finishing since the Security sheet needs
                // summary.account, which only exists once that's loaded.
                if sessionStore.pendingTwoFactorSetupEmail != nil {
                    showingSecurity = true
                }
                #if DEBUG
                // Same opt-in, env-var-gated debug hook as RootView's
                // auto-login — lets a screenshot-verification pass jump
                // straight to a specific sheet without needing a reliable
                // tap on this row. No-op unless explicitly set.
                switch ProcessInfo.processInfo.environment["CAVNAR_DEBUG_OPEN_SHEET"] {
                case "profile": showingProfile = true
                case "security": showingSecurity = true
                case "alerts": showingAlerts = true
                case "connections": showingConnections = true
                case "billing": showingBilling = true
                case "team": showingTeam = true
                case "staff": showingStaff = true
                case "export": showingExportData = true
                case "emailhistory": showingEmailHistory = true
                case "close-account": showingCloseAccount = true
                case "help": showingHelp = true
                case "referral": showingReferral = true
                default: break
                }
                #endif
            }
        }
    }

    /// Opens the sheet `account/<section>` names, once Account has loaded.
    /// The account owner's login (User.isOwner — permissions.TEAM_INVITE).
    private var isOwner: Bool { sessionStore.currentUser?.isOwner == true }

    /// Delete my login is offered on the server's word (`can_delete_login`);
    /// an older server that doesn't say offers it to everyone but an owner.
    static func offersDeleteLogin(canDeleteLogin: Bool?, isOwner: Bool) -> Bool {
        canDeleteLogin ?? !isOwner
    }

    private func openLinkedSection() {
        guard viewModel.summary != nil, deepLinkRouter.pendingAccountSection != nil,
              let section = deepLinkRouter.consumePendingAccountSection() else { return }
        switch AccountLinkSection(section) {
        case .profile: showingProfile = true
        // Manage team and Staff accounts are the owner's, as their rows are
        // (re-audit M7) — a teammate's link opens nothing rather than a
        // sheet of controls the server refuses.
        case .team: showingTeam = isOwner
        case .staff: showingStaff = isOwner
        case .people: showingPeople = true
        case .billing: showingBilling = isOwner
        case .alerts: showingAlerts = true
        case .automation: showingAutomation = true
        case .connections: showingConnections = true
        case .security: showingSecurity = true
        // The export is the web's now, as the row on the page is (M8).
        case .export: openWeb("account/data")
        case .help: showingHelp = true
        case .recommendations: showingRecommendations = true
        case .memory: showingMemory = true
        case nil: break
        }
    }

    /// Opens the web dashboard at `path` in the in-app browser — the same
    /// page a `CavnarWebLinkRow` opens, for a row or a link that isn't one.
    private func openWeb(_ path: String) {
        webTarget = CavnarHandoff.webpageURL(for: CavnarWebLinkRow.navPath(path)).map(AccountWebTarget.init(url:))
    }

    /// Whether this login can open what a health item's Fix names: people
    /// (Manage team) and subscription (Billing) are the owner's (M5).
    private func canOpenHealthFix(_ key: String) -> Bool {
        switch key {
        case "people", "subscription": return isOwner
        default: return true
        }
    }

    /// Mirrors the hero row and the first few setting groups so the real
    /// content lands into space already reserved for it.
    private var loadingSkeleton: some View {
        VStack(alignment: .leading, spacing: 24) {
            HStack(spacing: 14) {
                RoundedRectangle(cornerRadius: 20)
                    .fill(Color.cavnarInk.opacity(0.08))
                    .frame(width: 66, height: 66)
                VStack(alignment: .leading, spacing: 6) {
                    Capsule().fill(Color.cavnarInk.opacity(0.08)).frame(width: 168, height: 16)
                    Capsule().fill(Color.cavnarInk.opacity(0.06)).frame(width: 210, height: 13)
                }
                Spacer(minLength: 0)
            }
            .padding(.bottom, 6)

            ForEach(0..<3, id: \.self) { _ in
                VStack(alignment: .leading, spacing: 10) {
                    Capsule().fill(Color.cavnarInk.opacity(0.06)).frame(width: 86, height: 11)
                    RoundedRectangle(cornerRadius: 16)
                        .fill(Color.cavnarPaper2)
                        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                        .frame(height: 54)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .transition(.opacity)
    }

    @ViewBuilder
    private func content(_ summary: AccountSummary) -> some View {
        heroIdentity(summary)
        // Account health, the modules on the plan and the measured value —
        // the web's Account overview, scored on the server (parity #89).
        if let health = viewModel.health {
            AccountHealthCard(health: health, onFix: { key in openHealthFix(key) },
                              canFix: { key in canOpenHealthFix(key) })
        }
        groupedSettings(summary)
        signOutSection
    }

    /// The health card's Fix link (and each item's row) opens the sheet
    /// that fixes it; a teammate's people item has nothing to open.
    private func openHealthFix(_ key: String) {
        switch key {
        case "profile": showingProfile = true
        case "people": if isOwner { showingTeam = true }
        case "integrations": showingConnections = true
        case "notifications": showingAlerts = true
        case "security": showingSecurity = true
        case "subscription": if isOwner { showingBilling = true }
        default: break
        }
    }

    // MARK: - Hero identity

    private func initials(_ name: String) -> String {
        let words = name.split(separator: " ")
        let letters = words.prefix(2).compactMap { $0.first }
        return String(letters).uppercased()
    }

    private func heroIdentity(_ summary: AccountSummary) -> some View {
        // Who and which restaurant — nothing else. The billing status pill
        // that sat here repeated the Billing row (iOS readability round [70]).
        HStack(spacing: CavnarSpace.m) {
            GlowBadge(systemImage: "", size: 66, monogram: initials(summary.profile.restaurantName))

            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                Text(summary.profile.restaurantName)
                    .cavnarText(.headline)
                    .lineLimit(2)
                    .minimumScaleFactor(0.85)
                Text("\(summary.account.email) · \(sessionStore.currentUser?.isOwner == true ? "Owner" : "Team member")")
                    .cavnarText(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
            }

            Spacer(minLength: 0)
        }
        .padding(.bottom, 6)
    }

    // MARK: - Grouped settings

    @State private var showingProfile = false
    @State private var showingAutomation = false
    @State private var showingMemory = false
    @State private var showingRecommendations = false
    @State private var showingSecurity = false
    @State private var showingAlerts = false
    @State private var showingConnections = false
    @State private var showingBilling = false
    @State private var showingChangelog = false
    @State private var showingTeam = false
    @State private var showingStaff = false
    /// Account → People: house rules, staff docs, certifications (parity #62).
    @State private var showingPeople = false
    /// Export and email history are web rows on the page now (iOS
    /// readability round); their sheets stay for an account/data link.
    @State private var showingExportData = false
    @State private var showingEmailHistory = false
    @State private var showingCloseAccount = false
    @State private var showingHelp = false
    @State private var showingReportBug = false
    @State private var showingReferral = false
    @State private var showingDeleteLogin = false
    @State private var showingViewAs = false
    @State private var prefs = AppPreferences.shared
    /// The targets the Restaurant group reads (one line, read only — the
    /// numbers are set on the web, iOS readability round [71]).
    @State private var targets = AccountTargetsModel()

    /// The App row's sheet: haptics, the tab it opens on, the app icon (L8).
    @State private var showingAppSettings = false
    /// A web page opened from a row or a link that isn't a CavnarWebLinkRow
    /// (Targets, an account/data link).
    @State private var webTarget: AccountWebTarget?

    struct AccountWebTarget: Identifiable {
        let url: URL
        var id: String { url.absoluteString }
    }

    /// Every sheet Account opens — closing any of them may have fixed a
    /// health item, so the card re-reads (re-audit L4: Automation, Memory,
    /// People, Close account and the rest were missing).
    private var anySheetOpen: Bool {
        showingProfile || showingSecurity || showingAlerts || showingConnections || showingBilling
            || showingTeam || showingStaff || showingPeople || showingAutomation || showingMemory
            || showingRecommendations || showingExportData || showingEmailHistory || showingCloseAccount
            || showingDeleteLogin || showingHelp || showingReportBug || showingReferral || showingChangelog
            || showingAppSettings || showingViewAs || webTarget != nil
    }
    @State private var changelogBadge = ChangelogBadgeViewModel()

    /// Four groups (iOS readability round [70]): Restaurant · Notifications
    /// & security · Team · App & support, then the account's own data. A
    /// group of one row whose kicker repeated the row is gone; each card's
    /// rows are divided the same way.
    private func groupedSettings(_ summary: AccountSummary) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xl) {
            // Cavnar AI's own login only — the web console's View as client
            // (10/8/26). Hidden while a view is open: the banner ends it.
            if sessionStore.currentUser?.isInternal == true, sessionStore.viewAs == nil {
                group("Cavnar AI admin") {
                    settingsRow {
                        row("View as a client", systemImage: "eye")
                    } action: {
                        showingViewAs = true
                    }
                }
                .sheet(isPresented: $showingViewAs) {
                    AdminViewAsSheet()
                }
            }
            group("Restaurant") {
                settingsRow {
                    row("Profile & details", systemImage: "building.2")
                } action: {
                    showingProfile = true
                }
                rowDivider
                settingsRow {
                    // Every connection, RPOWER and website analytics included
                    // (parity #80: "X of 5" left them out).
                    row("Connected apps", systemImage: "link",
                        trailing: "\(summary.connections.connectedCount) of \(summary.connections.all.count)")
                } action: {
                    showingConnections = true
                }
                rowDivider
                settingsRow {
                    row("Automation & trust", systemImage: "sparkles.rectangle.stack")
                } action: {
                    showingAutomation = true
                }
                rowDivider
                // What Cavnar AI remembers, who said it, who may read it,
                // and what left without anyone asking (memory round, M2).
                settingsRow {
                    row("Memory", systemImage: "brain.head.profile")
                } action: {
                    showingMemory = true
                }
                rowDivider
                // What Cavnar AI recommended, what the owner did about it,
                // and what it did — the web's "What you've decided", with
                // rates, results and check-ins (rec-ROI #13, #20).
                settingsRow {
                    row("Recommendations", systemImage: "checklist")
                } action: {
                    showingRecommendations = true
                }
                rowDivider
                // Labor and food targets, read only; pay by role, salaried
                // staff, revenue and the rest are set on the web (iOS
                // readability round [71], the web's Targets & pay rates).
                targetsRow
                // The account owner's alone, as the server holds it (billing
                // and closing the account answer 403 owner_only to a teammate).
                if isOwner {
                    rowDivider
                    settingsRow {
                        row("Plan & billing", systemImage: "creditcard", trailing: billingStatusWord)
                    } action: {
                        showingBilling = true
                    }
                }
            }
            .sheet(isPresented: $showingProfile) {
                AccountProfileDetailView(viewModel: viewModel, profile: summary.profile)
            }
            .sheet(isPresented: $showingConnections) {
                AccountConnectionsDetailView(viewModel: viewModel, connections: summary.connections)
            }
            .sheet(isPresented: $showingAutomation) {
                AccountAutomationView()
            }
            .sheet(isPresented: $showingMemory) {
                AccountMemoryView()
            }
            .sheet(isPresented: $showingRecommendations) {
                RecommendationHistoryView()
            }
            .sheet(isPresented: $showingBilling) {
                AccountBillingDetailView(viewModel: viewModel, billing: viewModel.billing)
            }
            .task { await targets.load() }

            group("Notifications & security") {
                settingsRow {
                    row("Notifications", systemImage: "bell")
                } action: {
                    showingAlerts = true
                }
                rowDivider
                settingsRow {
                    row(
                        "Security & devices", systemImage: "lock.shield",
                        // "3 devices", not a bare "3" beside 2FA ON (L12).
                        trailing: viewModel.sessions.isEmpty ? nil
                            : "\(viewModel.sessions.count) device\(viewModel.sessions.count == 1 ? "" : "s")",
                        badge: summary.account.twoFAEnabled ? "2FA ON" : nil
                    )
                } action: {
                    showingSecurity = true
                }
            }
            .sheet(isPresented: $showingAlerts) {
                AccountAlertsDetailView(viewModel: viewModel, alerts: summary.alerts)
            }
            .sheet(isPresented: $showingSecurity) {
                AccountSecurityDetailView(viewModel: viewModel, account: summary.account)
            }

            group("Team") {
                // Only the account's owner login can invite/remove other logins
                // on this restaurant — matches mobile_api.py's server-side
                // role=='owner' check on the invite/revoke routes, which is the
                // actual enforcement; hiding the row for a teammate just avoids
                // showing a control that would 403 anyway.
                if sessionStore.currentUser?.isOwner == true {
                    settingsRow {
                        row("Manage team", systemImage: "person.2")
                    } action: {
                        showingTeam = true
                    }
                    rowDivider
                    // Employees, as distinct from teammates: PIN identities on
                    // the staff portal, who mostly sign themselves up with the
                    // join code. Same owner-only gate, enforced server-side by
                    // permissions.TEAM_INVITE.
                    settingsRow {
                        row("Staff accounts", systemImage: "person.badge.key")
                    } action: {
                        showingStaff = true
                    }
                    rowDivider
                }
                // What staff can look up in the app — house rules, docs by job,
                // certifications with the expiry reminder — for every login the
                // server answers for (parity #62).
                settingsRow {
                    row("House rules, docs & certificates", systemImage: "person.text.rectangle")
                } action: {
                    showingPeople = true
                }
            }
            .sheet(isPresented: $showingTeam) {
                AccountTeamDetailView(viewModel: viewModel)
            }
            .sheet(isPresented: $showingStaff) {
                AccountStaffDetailView(viewModel: viewModel)
            }
            .sheet(isPresented: $showingPeople) {
                AccountPeopleRulesView(accountViewModel: viewModel, isOwner: isOwner)
            }

            group("App & support") {
                // Haptics, the tab the app opens on and the home-screen icon
                // in one row (re-audit L8: nine rows in this card).
                settingsRow {
                    row("App", systemImage: "iphone", trailing: "Opens on \(prefs.defaultTab.title)")
                } action: {
                    showingAppSettings = true
                }
                rowDivider
                settingsRow {
                    row(
                        "What's New", systemImage: "sparkles",
                        trailing: changelogBadge.unreadCount > 0 ? "\(changelogBadge.unreadCount)" : nil
                    )
                } action: {
                    showingChangelog = true
                }
                rowDivider
                // The system's review prompt: there is no App Store ID in the
                // project yet for a write-review link (re-audit L1 — switch
                // to apps.apple.com/app/id<ID>?action=write-review once the
                // listing has one).
                settingsRow {
                    row("Rate Cavnar AI", systemImage: "star")
                } action: {
                    if let scene = UIApplication.shared.connectedScenes.first(where: { $0.activationState == .foregroundActive }) as? UIWindowScene {
                        AppStore.requestReview(in: scene)
                    }
                }
                // "Contact Will" is gone: Help & FAQ opens on Will's card,
                // with Email and Book a call (L8).
                // Referrals are the account holder's (/send-referral answers
                // 403 to anyone else), as on the web.
                if isOwner {
                    rowDivider
                    settingsRow {
                        row("Refer a restaurant", systemImage: "gift")
                    } action: {
                        showingReferral = true
                    }
                }
                rowDivider
                settingsRow {
                    row("Report a bug", systemImage: "ladybug")
                } action: {
                    showingReportBug = true
                }
                rowDivider
                settingsRow {
                    row("Help & FAQ", systemImage: "questionmark.circle")
                } action: {
                    showingHelp = true
                }
            }
            .task { await changelogBadge.refresh() }
            .sheet(isPresented: $showingChangelog) {
                ChangelogView()
            }
            .sheet(isPresented: $showingHelp) {
                AccountHelpView()
            }
            .sheet(isPresented: $showingAppSettings) {
                AccountAppSettingsSheet()
            }
            .sheet(isPresented: $showingReportBug) {
                AccountReportBugSheet(viewModel: viewModel)
            }
            .sheet(isPresented: $showingReferral) {
                AccountReferralView(viewModel: viewModel)
            }

            // The account's records and its end. Schedule history, email
            // history and the data export are read on the web (iOS
            // readability round); closing the account and deleting a login
            // stay in the app (App Store Guideline 5.1.1(v)).
            group("Your data") {
                // The Studio's History stage and the Email history card,
                // opened (re-audit M1/M2) — they landed on the schedule and
                // the top of Notifications.
                webRow("Schedule history", path: "labor/history")
                rowDivider
                webRow("Email history", path: "account/email-history")
                rowDivider
                webRow("Export my data", path: "account/data")
                if isOwner {
                    rowDivider
                    settingsRow {
                        row("Close my account", systemImage: "xmark.circle")
                    } action: {
                        showingCloseAccount = true
                    }
                }
                // A teammate deletes their own login (Guideline 5.1.1(v),
                // parity #13), and so may a co-owner while another owner
                // remains (re-audit 10/8/26, #7) — the server's rule.
                if Self.offersDeleteLogin(canDeleteLogin: viewModel.summary?.account.canDeleteLogin, isOwner: isOwner) {
                    rowDivider
                    settingsRow {
                        row("Delete my login", systemImage: "person.crop.circle.badge.xmark")
                    } action: {
                        showingDeleteLogin = true
                    }
                }
            }
            .sheet(isPresented: $showingEmailHistory) {
                AccountEmailHistoryView(viewModel: viewModel)
            }
            .sheet(isPresented: $showingExportData) {
                AccountExportDataView(viewModel: viewModel)
            }
            .sheet(isPresented: $showingCloseAccount) {
                AccountCloseAccountView(viewModel: viewModel)
            }
            .sheet(isPresented: $showingDeleteLogin) {
                AccountDeleteLoginView(viewModel: viewModel)
            }
        }
    }

    /// One hairline between rows, inset past the icon — every card the same.
    private var rowDivider: some View {
        Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1).padding(.leading, 47)
    }

    /// "Active", "Trial", "Past due" — the plan's state, no figures (the
    /// amount and the next charge are on the Billing sheet, once).
    private var billingStatusWord: String? {
        guard let billing = viewModel.billing, billing.ok, let status = billing.status else { return nil }
        switch status {
        case "active": return "Active"
        case "trialing": return "Trial"
        case "past_due": return "Past due"
        case "inactive": return nil
        default: return status.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    /// "Targets · Labor 28% · Food 30% ›" — one row that opens the web's
    /// Targets & pay rates, where they're set (re-audit L11: a read-only row
    /// and a second web row said the same thing).
    private var targetsRow: some View {
        settingsRow {
            row("Targets", systemImage: "target",
                trailing: Self.targetsLine(targets.payload) ?? (targets.isLoading ? nil : "Not set"))
        } action: {
            openWeb("account/restaurant")
        }
        .accessibilityHint("Opens Targets & pay rates on the web")
    }

    /// "Labor 28% · Food 30%" from the targets payload; nil when neither is set.
    static func targetsLine(_ p: TargetsPayload?) -> String? {
        guard let p else { return nil }
        func pct(_ v: Double) -> String { v == v.rounded() ? "\(Int(v))%" : String(format: "%.1f%%", v) }
        var parts: [String] = []
        if let l = p.laborTargetPct { parts.append("Labor " + pct(l)) }
        if let f = p.foodCostTarget { parts.append("Food " + pct(f)) }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }

    /// A row that opens the web dashboard at `path` (L3), inset like the
    /// rows around it.
    private func webRow(_ title: String, path: String) -> some View {
        CavnarWebLinkRow(title: title, path: path, actionLabel: "On the web")
            .padding(.horizontal, CavnarSpace.m)
            .frame(minHeight: 54)
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

    private func group<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs + 2) {
            // The one kicker (CavnarKicker), the same as inside the Account
            // sheets and the web's `.ac-kicker`.
            CavnarKicker(title)
            VStack(spacing: 0) { content() }
                .background(Color.cavnarPaper2)
                .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: 16))
        }
    }

    private func row(_ label: String, systemImage: String, trailing: String? = nil, badge: String? = nil) -> some View {
        HStack(spacing: 13) {
            Image(systemName: systemImage)
                .font(.cavnar(.body))
                .foregroundStyle(Color.cavnarInk3)
                .frame(width: 18)
                .accessibilityHidden(true)
            Text(label)
                .cavnarText(.body, color: .cavnarInk)
            Spacer()
            // A status pill (2FA on/off) is a different kind of signal
            // than the plain count `trailing` shows elsewhere — its own
            // tinted capsule so it reads at a glance.
            if let badge {
                HStack(spacing: 4) {
                    Circle().fill(Color.cavnarGreen).frame(width: 6, height: 6)
                    Text(badge)
                        .font(.cavnarBody(CavnarType.tag, weight: 700))
                        .foregroundStyle(Color.cavnarGreen)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color.cavnarGreen.opacity(0.14))
                .clipShape(Capsule())
            }
            if let trailing {
                HomeMixedText.make(trailing, role: .secondary)
            }
            AccountDisclosureChip()
        }
        .padding(.horizontal, CavnarSpace.m)
        // minHeight, not height — see AccountKVRow (audit 7.2). Also keeps
        // these rows comfortably above the 44pt HIG tap-target minimum.
        .frame(minHeight: 54)
        .contentShape(Rectangle())
    }

    // MARK: - Sign out

    @ViewBuilder
    private var signOutSection: some View {
        // Viewing as a client, this is the client's Account: the way out is
        // back to the admin's own login, never a sign-out of either.
        if let view = sessionStore.viewAs {
            Button("Stop viewing as \(view.restaurantName)") {
                Haptic.light()
                Task { await sessionStore.stopViewAs() }
            }
            .font(.cavnar(.label))
            .foregroundStyle(Color.cavnarAmber)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 13)
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.cavnarAmber.opacity(0.45), lineWidth: 1))
            .padding(.top, 4)
        } else {
            signOutButton
        }
    }

    private var signOutButton: some View {
        Button("Sign Out", role: .destructive) {
            Haptic.light()
            Task { await sessionStore.logout() }
        }
        .font(.cavnar(.label))
        .foregroundStyle(Color.cavnarRedText)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 13)
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.cavnarRed.opacity(0.35), lineWidth: 1))
        .padding(.top, 4)
    }
}

/// nav.py's Account sections (command_center._ACCOUNT_SECTIONS, the
/// notification map's "account/security" …) → the app's Account sheets.
/// Nil for a section the app shows on Account's page itself (appearance).
enum AccountLinkSection: Equatable {
    case profile, team, billing, alerts, automation, connections, security, export, help, recommendations
    /// Account → Staff accounts (re-audit M7: "staff" opened Manage team).
    case staff
    /// Account → People: house rules, docs, certifications and, for the
    /// owner, access (the certificate expiry email's link; parity #62).
    case people
    /// Account → Memory (memory round 9/29/26).
    case memory

    init?(_ raw: String) {
        switch raw.lowercased() {
        case "restaurant", "profile": self = .profile
        case "people": self = .people
        case "team": self = .team
        case "staff": self = .staff
        case "billing": self = .billing
        case "notifications", "alerts": self = .alerts
        case "automation": self = .automation
        case "integrations", "connections": self = .connections
        case "security": self = .security
        case "data", "export": self = .export
        case "support", "help": self = .help
        case "recs", "recommendations": self = .recommendations
        case "memory", "remembers": self = .memory
        default: return nil
        }
    }
}

/// Account → App: the phone's own preferences — haptics, the tab the app
/// opens on, and the home-screen icon — folded from three rows of App &
/// support into one (re-audit L8).
struct AccountAppSettingsSheet: View {
    @State private var prefs = AppPreferences.shared
    // The HOME-SCREEN icon only — black-with-cream-seal (default) vs.
    // white-with-black-seal (AppIconLight, registered in project.yml's
    // CFBundleAlternateIcons). The in-app interface stays dark-only
    // regardless (see RootView) — this is purely UIApplication's own
    // alternate-icon mechanism, not our own preference storage, so the
    // switch always reflects whatever's actually active rather than
    // trusting a stale local flag if the change silently failed.
    @State private var isLightAppIcon = UIApplication.shared.alternateIconName == "AppIconLight"
    @State private var appIconError: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    AccountHero(title: "App") {
                        GlowBadge(systemImage: "iphone", size: 64)
                    } subtitle: {
                        Text("How Cavnar AI behaves on this phone")
                    }
                    AccountSection(kicker: "This phone") {
                        AccountSwitchRow(label: "Haptic feedback", isOn: Binding(
                            get: { prefs.hapticsEnabled },
                            set: { on in prefs.hapticsEnabled = on; if on { Haptic.light() } }
                        ))
                        AccountKVRow(label: "Open on") {
                            Picker("Open on", selection: Binding(
                                get: { prefs.defaultTab },
                                set: { tab in Haptic.selection(); prefs.defaultTab = tab }
                            )) {
                                ForEach(AppTab.allCases) { Text($0.title).tag($0) }
                            }
                            .labelsHidden()
                            .tint(Color.cavnarEmber)
                        }
                        AccountSwitchRow(label: "Light app icon", isOn: Binding(
                            get: { isLightAppIcon },
                            set: { newValue in setAppIcon(light: newValue) }
                        ), optimistic: false, showsDivider: false)
                        if let appIconError {
                            Text(appIconError)
                                .cavnarText(.secondary, color: .cavnarRedText)
                                .padding(.bottom, 6)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("App")
        }
    }

    private func setAppIcon(light: Bool) {
        guard UIApplication.shared.supportsAlternateIcons else {
            appIconError = "This device doesn't support switching app icons."
            return
        }
        appIconError = nil
        let targetName = light ? "AppIconLight" : nil
        UIApplication.shared.setAlternateIconName(targetName) { error in
            Task { @MainActor in
                if let error {
                    appIconError = error.localizedDescription
                    // Reflect whatever's actually active, not the tap's
                    // intent — a failed switch snaps back rather than show a
                    // state that didn't apply.
                    isLightAppIcon = UIApplication.shared.alternateIconName == "AppIconLight"
                } else {
                    isLightAppIcon = light
                }
            }
        }
    }
}
