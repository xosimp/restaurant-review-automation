import SwiftUI

/// Opened from Security's "Account activity" row. Everything that changed
/// on the account — password, email, 2FA, team, exports, alert settings —
/// with who did it and when. Sign-ins themselves live in Sign-in activity;
/// this is the "what changed" half.
struct AccountActivityLogView: View {
    let viewModel: AccountViewModel

    private func symbol(for event: AccountActivityEvent) -> String {
        switch event.type {
        case "password_changed": return "key.fill"
        case "email_changed", "recovery_email_set", "recovery_email_removed": return "envelope.fill"
        case "two_fa_enabled", "two_fa_disabled", "backup_codes_regenerated": return "lock.shield.fill"
        case "team_member_invited", "team_member_revoked": return "person.2.fill"
        case "sessions_revoked_others", "trusted_device_revoked", "trusted_devices_cleared", "login_reported_not_me": return "iphone.slash"
        case "data_exported", "data_retention_changed": return "square.and.arrow.up.fill"
        case "alert_settings_saved", "login_notify_changed", "marketing_emails_changed": return "bell.fill"
        case "auto_approve_changed": return "checkmark.seal.fill"
        case "hours_changed": return "clock.fill"
        default: return "circle.fill"
        }
    }

    private func isSecuritySensitive(_ event: AccountActivityEvent) -> Bool {
        ["login_reported_not_me", "two_fa_disabled", "trusted_devices_cleared", "sessions_revoked_others"].contains(event.type)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Account activity") {
                        GlowBadge(systemImage: "list.bullet.rectangle", size: 64)
                    } subtitle: {
                        Text("Every change to your account, and who made it")
                    }

                    if viewModel.isLoadingActivity && viewModel.activity.isEmpty {
                        CavnarLoadingSeal().padding(.top, 40).frame(maxWidth: .infinity)
                    } else if viewModel.activity.isEmpty {
                        Text("Nothing has changed on your account yet.")
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk3)
                            .padding(.top, 20)
                            .frame(maxWidth: .infinity)
                    } else {
                        AccountSection(kicker: "Most recent first") {
                            ForEach(Array(viewModel.activity.enumerated()), id: \.element.id) { index, event in
                                HStack(alignment: .top, spacing: 12) {
                                    Image(systemName: symbol(for: event))
                                        .font(.system(size: 15, weight: .medium))
                                        .foregroundStyle(isSecuritySensitive(event) ? Color.cavnarRed : Color.cavnarEmber2)
                                        .frame(width: 34, height: 34)
                                        .background(Color.white.opacity(0.05))
                                        .clipShape(RoundedRectangle(cornerRadius: 10))
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(event.label).font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                                        HStack(spacing: 6) {
                                            Text(AccountRelativeTime.describe(event.createdAt))
                                                .font(.cavnarNumber(14))
                                            if let actor = event.actor, !actor.isEmpty {
                                                (Text("· by ").font(.cavnarBody(14))
                                                    + Text(actor).font(.cavnarBody(14, weight: 700)).foregroundStyle(Color.cavnarEmber2))
                                            }
                                        }
                                        .foregroundStyle(Color.cavnarInk3)
                                        if let detail = event.detail, !detail.isEmpty {
                                            Text(detail).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2)
                                        }
                                    }
                                    Spacer(minLength: 0)
                                }
                                .padding(.vertical, 10)
                                if index < viewModel.activity.count - 1 { AccountRowDivider() }
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Account Activity")
            .task { await viewModel.loadActivity() }
        }
    }
}
