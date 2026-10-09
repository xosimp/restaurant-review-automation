import SwiftUI

/// Opened from Security's "Activity" row. Everything that changed on the
/// account — password, email, 2FA, team, exports, alert settings — with who
/// did it and when, and the sign-ins one tap in ("Sign-ins"): Sign-in
/// activity and Account activity were two rows (iOS readability round).
struct AccountActivityLogView: View {
    let viewModel: AccountViewModel
    @State private var showingSignIns = false
    /// The most recent few of each list; the rest are on the web.
    static let shown = 3

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
                    AccountHero(title: "Activity") {
                        GlowBadge(systemImage: "list.bullet.rectangle", size: 64)
                    } subtitle: {
                        Text("Every change to your account, and who made it")
                    }

                    VStack(alignment: .leading, spacing: 0) {
                        AccountNavRow(label: "Sign-ins", showsDivider: false) { showingSignIns = true }
                    }
                    .accountCard()

                    // What changed on the restaurant — targets, settings,
                    // never-say, hours, prices, menu, roster — who changed
                    // it and when, kept for good (change_log, M7). Pay
                    // changes reach an account holder only (the server's).
                    // The last three of each here; the full history is the
                    // web's Security section (re-audit M6).
                    if !viewModel.changes.isEmpty {
                        AccountSection(kicker: "Settings and targets") {
                            ForEach(Array(viewModel.changes.prefix(Self.shown).enumerated()), id: \.element.id) { index, change in
                                HStack(alignment: .top, spacing: 12) {
                                    Image(systemName: change.symbol)
                                        .font(.system(size: 15, weight: .medium))
                                        .foregroundStyle(Color.cavnarEmber2)
                                        .frame(width: 34, height: 34)
                                        .background(Color.white.opacity(0.05))
                                        .clipShape(RoundedRectangle(cornerRadius: 10))
                                        .accessibilityHidden(true)
                                    CavnarMixedText(change.line, role: .body, color: .cavnarInk)
                                    Spacer(minLength: 0)
                                }
                                .padding(.vertical, 10)
                                if index < min(viewModel.changes.count, Self.shown) - 1 { AccountRowDivider() }
                            }
                        }
                    }

                    if viewModel.isLoadingActivity && viewModel.activity.isEmpty && viewModel.changes.isEmpty {
                        CavnarSkeletonBar(height: 3).padding(.vertical, 10)
                            .accessibilityLabel("Loading activity")
                    } else if viewModel.activity.isEmpty && viewModel.changes.isEmpty {
                        // Only when there's nothing at all — it rendered under
                        // a full Settings and targets list (re-audit M6).
                        Text("Nothing has changed on your account yet.")
                            .cavnarText(.body)
                            .padding(.top, 20)
                            .frame(maxWidth: .infinity)
                    } else if !viewModel.activity.isEmpty {
                        AccountSection(kicker: "Account and security") {
                            ForEach(Array(viewModel.activity.prefix(Self.shown).enumerated()), id: \.element.id) { index, event in
                                HStack(alignment: .top, spacing: 12) {
                                    Image(systemName: symbol(for: event))
                                        .font(.system(size: 15, weight: .medium))
                                        .foregroundStyle(isSecuritySensitive(event) ? Color.cavnarRed : Color.cavnarEmber2)
                                        .frame(width: 34, height: 34)
                                        .background(Color.white.opacity(0.05))
                                        .clipShape(RoundedRectangle(cornerRadius: 10))
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(event.label).cavnarText(.label)
                                        HStack(spacing: 6) {
                                            HomeMixedText.make(AccountRelativeTime.describe(event.createdAt), role: .secondary)
                                            if let actor = event.actor, !actor.isEmpty {
                                                (Text("· by ").font(.cavnar(.secondary))
                                                    + Text(actor).font(.cavnarBody(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarEmber2))
                                            }
                                        }
                                        .foregroundStyle(Color.cavnarInk2)
                                        if let detail = event.detail, !detail.isEmpty {
                                            Text(detail).cavnarText(.secondary)
                                        }
                                    }
                                    Spacer(minLength: 0)
                                }
                                .padding(.vertical, 10)
                                if index < min(viewModel.activity.count, Self.shown) - 1 { AccountRowDivider() }
                            }
                        }
                    }

                    if viewModel.activity.count > Self.shown || viewModel.changes.count > Self.shown {
                        VStack(alignment: .leading, spacing: 0) {
                            CavnarWebLinkRow(title: "The full history",
                                             subtitle: "Account activity and change history, every entry.",
                                             path: "account/security", actionLabel: "Open on the web")
                        }
                        .accountCard()
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Activity")
            .task { await viewModel.loadActivity() }
            .sheet(isPresented: $showingSignIns) { AccountSignInHistoryView(viewModel: viewModel) }
        }
    }
}

/// One lasting, attributed change (GET /account/activity `changes`,
/// change_log.for_viewer): `line` is the owner-facing sentence, dates
/// already M/D/YY.
struct AccountChange: Codable, Hashable, Identifiable {
    let line: String
    var kind: String? = nil
    var field: String? = nil
    var changedAt: String? = nil

    var id: String { (changedAt ?? "") + "|" + line }

    enum CodingKeys: String, CodingKey {
        case line, kind, field
        case changedAt = "changed_at"
    }

    init(line: String, kind: String? = nil, field: String? = nil, changedAt: String? = nil) {
        self.line = line; self.kind = kind; self.field = field; self.changedAt = changedAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let l = try c.decode(String.self, forKey: .line).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !l.isEmpty else {
            throw DecodingError.dataCorruptedError(forKey: .line, in: c, debugDescription: "empty line")
        }
        line = l
        kind = (try? c.decodeIfPresent(String.self, forKey: .kind)) ?? nil
        field = (try? c.decodeIfPresent(String.self, forKey: .field)) ?? nil
        changedAt = (try? c.decodeIfPresent(String.self, forKey: .changedAt)) ?? nil
    }

    /// A glyph for the kind of change; a kind the app doesn't know gets
    /// the plain one.
    var symbol: String {
        switch kind {
        case "price", "menu_add", "menu_remove", "menu_change": return "tag.fill"
        case "roster_add", "roster_leave", "roster_change": return "person.2.fill"
        case "pay": return "dollarsign.circle.fill"
        case "hours": return "clock.fill"
        case "supplier": return "shippingbox.fill"
        case "target": return "target"
        case "never_say", "voice": return "text.bubble.fill"
        case "notifications": return "bell.fill"
        case "automation": return "sparkles"
        case "rules": return "list.bullet"
        default: return "slider.horizontal.3"
        }
    }
}
