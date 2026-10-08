import SwiftUI

/// Opened from Security's "Security checkup" row. The score and its six
/// items are the server's (account_health.security_checkup, GET
/// /account/security-summary) — the same rule and the same number the web's
/// Security card shows; the app used to score seven items its own way and
/// the two disagreed (iOS parity matrix). This phone's re-entry lock is kept
/// as its own item below them: a setting of the phone, not of the account,
/// so it isn't part of the account's score. Each unearned item has a fix-it
/// link that hands the action back to the Security sheet.
struct AccountSecurityCheckupView: View {
    enum Fix { case twoFA, password, deviceLock, backupCodes, loginNotify, recoveryEmail, staleSessions }

    let viewModel: AccountViewModel
    let account: AccountInfo
    var onFix: (Fix) -> Void
    @Environment(SessionStore.self) private var sessionStore
    @Environment(\.dismiss) private var dismiss
    @State private var animatedScore: Double = 0

    /// The server item's fix, by its key.
    static func fix(for key: String) -> Fix? {
        switch key {
        case "two_fa": return .twoFA
        case "password": return .password
        case "backup_codes": return .backupCodes
        case "login_notify": return .loginNotify
        case "recovery_email": return .recoveryEmail
        case "devices": return .staleSessions
        default: return nil
        }
    }

    private var checkup: SecurityCheckup? { viewModel.securitySummary?.checkup }
    private var score: Int? { checkup?.score }

    private var scoreTone: Color {
        switch score ?? 0 {
        case 80...: return .cavnarGreen
        case 55..<80: return .cavnarAmber
        default: return .cavnarRed
        }
    }

    private var verdict: String {
        guard let score else { return "Security checkup" }
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
                        Text("The same checkup as on the web")
                    }

                    HStack(alignment: .lastTextBaseline, spacing: 6) {
                        if score != nil {
                            CavnarAnimatableNumber(value: animatedScore, format: { String(Int($0.rounded())) })
                                .font(.cavnarNumber(56, weight: 600))
                                .foregroundStyle(scoreTone)
                                .cavnarNumberGlow(scoreTone)
                        } else {
                            Text("\u{2014}").font(.cavnarNumber(56, weight: 600)).foregroundStyle(Color.cavnarInk3)
                        }
                        Text("/ \(checkup?.max ?? 100)")
                            .font(.cavnarNumber(18))
                            .foregroundStyle(Color.cavnarInk3)
                        Spacer()
                    }
                    .onChange(of: score) { _, s in
                        withAnimation(.easeOut(duration: 1.0)) { animatedScore = Double(s ?? 0) }
                    }

                    if let checkup {
                        AccountSection(kicker: "What's counted") {
                            ForEach(Array(checkup.items.enumerated()), id: \.element.id) { index, item in
                                itemRow(title: item.title, detail: item.detail, earned: item.earned,
                                        points: item.points, fix: item.earned ? nil : Self.fix(for: item.key),
                                        fixLabel: item.fixLabel ?? "Fix",
                                        showsDivider: index < checkup.items.count - 1)
                            }
                        }
                    } else {
                        CavnarSkeletonBar(height: 3)
                            .padding(.vertical, 10)
                            .accessibilityLabel("Loading the checkup")
                    }

                    // This phone — kept from the app's own checkup, outside
                    // the account's score.
                    AccountSection(kicker: "On this phone") {
                        itemRow(title: "Re-entry lock",
                                detail: sessionStore.reentryProtected
                                    ? (sessionStore.appPasscodeSet ? "Face ID and app passcode" : "Face ID")
                                    : "Off \u{2014} the app reopens without asking",
                                earned: sessionStore.reentryProtected, points: nil,
                                fix: sessionStore.reentryProtected ? nil : .deviceLock, fixLabel: "Set up",
                                showsDivider: false)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Security Checkup")
            .task {
                await viewModel.loadSecuritySummary()
                withAnimation(.easeOut(duration: 1.0)) { animatedScore = Double(score ?? 0) }
            }
        }
    }

    private func itemRow(title: String, detail: String, earned: Bool, points: Int?,
                         fix: Fix?, fixLabel: String, showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: earned ? "checkmark.circle.fill" : "exclamationmark.circle")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(earned ? Color.cavnarGreen : Color.cavnarAmber)
                    .frame(width: 24)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text(title).font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                        Spacer(minLength: 8)
                        if let points {
                            Text(earned ? "+\(points)" : "\(points) pts")
                                .font(.cavnarNumber(14, weight: 600))
                                .foregroundStyle(earned ? Color.cavnarGreen : Color.cavnarInk3)
                        }
                    }
                    HomeMixedText.make(detail, size: 14, color: .cavnarInk3)
                    if let fix {
                        Button {
                            Haptic.light()
                            dismiss()
                            onFix(fix)
                        } label: {
                            HStack(spacing: 8) {
                                Text(fixLabel).font(.cavnarBody(14, weight: 700)).foregroundStyle(Color.cavnarEmber)
                                AccountDisclosureChip()
                            }
                        }
                        .buttonStyle(.plain)
                        .padding(.top, 4)
                    }
                }
            }
            .padding(.vertical, 10)
            if showsDivider { AccountRowDivider() }
        }
    }
}
