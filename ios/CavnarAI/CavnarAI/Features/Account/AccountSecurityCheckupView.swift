import SwiftUI

/// Opened from Security's "Security checkup" row. One score from data the
/// account already has — nothing new is fetched — with a fix-it link per
/// item that hands the action back to the Security sheet.
struct AccountSecurityCheckupView: View {
    enum Fix { case twoFA, password, deviceLock, backupCodes, loginNotify, recoveryEmail, staleSessions }

    let viewModel: AccountViewModel
    let account: AccountInfo
    var onFix: (Fix) -> Void
    @Environment(SessionStore.self) private var sessionStore
    @Environment(\.dismiss) private var dismiss
    @State private var animatedScore: Double = 0

    private struct Item: Identifiable {
        let id: String
        let title: String
        let detail: String
        let points: Int
        let earned: Bool
        let fix: Fix?
        let fixLabel: String
    }

    private var staleSessionCount: Int {
        viewModel.sessions.filter { !$0.isCurrent && AccountRelativeTime.daysSince($0.lastActive) > 30 }.count
    }

    private var items: [Item] {
        let strength = account.passwordStrength ?? ""
        let strongPassword = strength == "strong" || strength == "good"
        let codesOK = !account.twoFAEnabled || (viewModel.backupCodesRemaining ?? 0) > 0
        return [
            Item(id: "2fa", title: "Two-factor authentication",
                 detail: account.twoFAEnabled ? "On — a code is needed on new devices" : "Off — a password alone gets in",
                 points: 25, earned: account.twoFAEnabled, fix: .twoFA, fixLabel: "Turn on"),
            Item(id: "password", title: "Password strength",
                 detail: strongPassword ? "Rated \(strength)" : (strength.isEmpty ? "Unrated — set a new one to score it" : "Rated \(strength)"),
                 points: 15, earned: strongPassword, fix: .password, fixLabel: "Change"),
            Item(id: "lock", title: "Re-entry lock on this phone",
                 detail: sessionStore.reentryProtected ? (sessionStore.appPasscodeSet ? "Face ID and app passcode" : "Face ID") : "Off — the app reopens without asking",
                 points: 20, earned: sessionStore.reentryProtected, fix: .deviceLock, fixLabel: "Set up"),
            Item(id: "codes", title: "Backup codes on hand",
                 detail: account.twoFAEnabled ? "\(viewModel.backupCodesRemaining ?? 0) unused" : "Only needed once 2FA is on",
                 points: 10, earned: codesOK, fix: account.twoFAEnabled ? .backupCodes : nil, fixLabel: "Regenerate"),
            Item(id: "notify", title: "Sign-in notifications",
                 detail: account.loginNotify ? "On — every new sign-in emails you" : "Off — you won't hear about new sign-ins",
                 points: 10, earned: account.loginNotify, fix: .loginNotify, fixLabel: "Turn on"),
            Item(id: "recovery", title: "Recovery email",
                 detail: account.recoveryEmail ?? "None — losing your sign-in email means losing the account",
                 points: 10, earned: account.recoveryEmail != nil, fix: .recoveryEmail, fixLabel: "Add"),
            Item(id: "sessions", title: "No stale devices",
                 detail: staleSessionCount == 0 ? "Every signed-in device was active in the last 30 days" : "\(staleSessionCount) device\(staleSessionCount == 1 ? "" : "s") idle for 30+ days",
                 points: 10, earned: staleSessionCount == 0, fix: .staleSessions, fixLabel: "Review"),
        ]
    }

    private var score: Int { items.filter(\.earned).map(\.points).reduce(0, +) }

    private var scoreTone: Color {
        switch score {
        case 80...: return .cavnarGreen
        case 55..<80: return .cavnarAmber
        default: return .cavnarRed
        }
    }

    private var verdict: String {
        switch score {
        case 90...: return "Locked down"
        case 80..<90: return "In good shape"
        case 55..<80: return "A few gaps"
        default: return "Needs attention"
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: verdict) {
                        GlowBadge(systemImage: "checkmark.shield", size: 64)
                    } subtitle: {
                        Text("Security checkup")
                    }

                    // The score — big, animated, ember-glowing like every other
                    // hero number in the app.
                    HStack(alignment: .lastTextBaseline, spacing: 6) {
                        CavnarAnimatableNumber(value: animatedScore, format: { String(Int($0.rounded())) })
                            .font(.cavnarNumber(56, weight: 600))
                            .foregroundStyle(scoreTone)
                            .cavnarNumberGlow(scoreTone)
                        Text("/ 100")
                            .font(.cavnarNumber(18))
                            .foregroundStyle(Color.cavnarInk3)
                        Spacer()
                    }
                    .onAppear {
                        withAnimation(.easeOut(duration: 1.0)) { animatedScore = Double(score) }
                    }

                    AccountSection(kicker: "What's counted") {
                        ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                            HStack(alignment: .top, spacing: 12) {
                                Image(systemName: item.earned ? "checkmark.circle.fill" : "exclamationmark.circle")
                                    .font(.system(size: 18, weight: .semibold))
                                    .foregroundStyle(item.earned ? Color.cavnarGreen : Color.cavnarAmber)
                                    .frame(width: 24)
                                    .padding(.top, 1)
                                VStack(alignment: .leading, spacing: 3) {
                                    HStack {
                                        Text(item.title).font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                                        Spacer(minLength: 8)
                                        Text(item.earned ? "+\(item.points)" : "\(item.points) pts")
                                            .font(.cavnarNumber(14, weight: 600))
                                            .foregroundStyle(item.earned ? Color.cavnarGreen : Color.cavnarInk3)
                                    }
                                    Text(item.detail).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                                    if !item.earned, let fix = item.fix {
                                        AccountLink(title: item.fixLabel) {
                                            dismiss()
                                            onFix(fix)
                                        }
                                        .padding(.top, 2)
                                    }
                                }
                            }
                            .padding(.vertical, 10)
                            if index < items.count - 1 { AccountRowDivider() }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Security Checkup")
            .task {
                if account.twoFAEnabled { await viewModel.loadBackupCodesStatus() }
                if viewModel.sessions.isEmpty { await viewModel.loadSessions() }
            }
        }
    }
}
