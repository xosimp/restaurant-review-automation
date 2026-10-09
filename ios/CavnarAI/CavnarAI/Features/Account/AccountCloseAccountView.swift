import SwiftUI

/// Opened from Account's "Close my account" row. Not self-serve deletion —
/// clients sign a contract (DocuSign) to start service, so Cavnar AI can't
/// deactivate one on its own — but tapping the button below is a real
/// request, not a mailto: link: it hits the server, is recorded, and emails
/// Will to start the 30-day wind-down. That distinction is Apple App Store
/// Review Guideline 5.1.1(v) — an app that supports account creation must
/// let the user initiate deletion from inside the app; a "please email us"
/// flow doesn't satisfy it outside a handful of regulated industries, which
/// this isn't. A direct mailto to Will stays underneath as one more way to
/// reach him, same tappable-link pattern used elsewhere in Account, but the
/// primary control above it is the actual initiation.
struct AccountCloseAccountView: View {
    let viewModel: AccountViewModel
    @State private var showingConfirm = false

    private var mailtoLink: String {
        let name = viewModel.summary?.profile.restaurantName ?? "my restaurant"
        let subject = "Cancel my Cavnar AI subscription"
        let body = "Hi Will,\n\nI would like to cancel my Cavnar AI subscription for \(name).\n\nPer the 30-day notice policy, I understand my account will remain active through the end of my current billing period and for 30 days after this notice."
        var components = URLComponents()
        components.scheme = "mailto"
        components.path = "will@cavnar.ai"
        components.queryItems = [
            URLQueryItem(name: "subject", value: subject),
            URLQueryItem(name: "body", value: body),
        ]
        return components.string ?? "mailto:will@cavnar.ai"
    }

    private var requestedAt: String? { viewModel.summary?.profile.deletionRequestedAt }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Close my account") {
                        GlowBadge(systemImage: "xmark.circle", size: 64)
                    } subtitle: {
                        Text(requestedAt != nil ? "Request received" : "Cancellation goes through Will")
                    }

                    // The smaller decision, named first — the pause itself is
                    // on Plan & billing, its one place (re-audit L19).
                    Text("Need a break instead? Pausing stops billing and the briefs and resumes on its own \u{2014} it\u{2019}s on Plan & billing.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    // Two sentences (iOS readability round — it was 60 words).
                    Text("Your service agreement needs 30 days' written notice, so canceling goes through Will. Your account stays on through the end of this billing period plus 30 days after you ask.")
                        .cavnarText(.body)
                        .fixedSize(horizontal: false, vertical: true)

                    if let requestedAt {
                        AccountSection(kicker: "Status") {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("Deletion requested").font(.cavnarBody(CavnarType.body, weight: 700)).foregroundStyle(Color.cavnarInk)
                                Text("Will was notified on \(CavnarDate.mdy(requestedAt)). He'll reach out to confirm and start winding things down.")
                                    .font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2)
                            }
                            .padding(.vertical, 9)
                        }
                    } else {
                        if let error = viewModel.deletionRequestError {
                            Text(error).font(.cavnar(.body)).foregroundStyle(Color.cavnarRedText)
                        }
                        Button(role: .destructive) {
                            showingConfirm = true
                        } label: {
                            Group {
                                if viewModel.isRequestingDeletion {
                                    CavnarShimmerText(text: "Sending…")
                                } else {
                                    Text("Request account deletion")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isRequestingDeletion))
                        .disabled(viewModel.isRequestingDeletion)
                        .confirmationDialog(
                            "Request account deletion?",
                            isPresented: $showingConfirm,
                            titleVisibility: .visible
                        ) {
                            Button("Request deletion", role: .destructive) {
                                Task {
                                    if await viewModel.requestAccountDeletion() { Haptic.success() }
                                }
                            }
                            Button("Cancel", role: .cancel) {}
                        } message: {
                            Text("Will is notified right away to start the 30-day wind-down. Your account and data stay active until then.")
                        }
                    }

                    // A real Link, matching Help & FAQ's proven-working
                    // "Contact Will" and Billing's identical fix — markdown
                    // links embedded in Text never actually became tappable
                    // on a real device despite looking and coloring
                    // correctly, across multiple attempts. Link owns the tap
                    // gesture itself, so Text+Text concatenation for
                    // per-segment color is safe here.
                    if let url = URL(string: mailtoLink) {
                        Link(destination: url) {
                            Text(requestedAt != nil ? "Or contact " : "You can also contact ").foregroundStyle(Color.cavnarInk2)
                                + Text("will@cavnar.ai").foregroundStyle(Color.cavnarEmber2)
                                + Text(" directly.").foregroundStyle(Color.cavnarInk2)
                        }
                        .font(.cavnar(.body))
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Close My Account")
        }
    }
}


// MARK: - Delete my login (a teammate's)

/// Account → Delete my login, for a login that isn't the account holder's
/// (App Store Guideline 5.1.1(v), iOS parity #13). Close my account is the
/// owner's — the service is under their signed agreement — so a manager or
/// teammate could not delete the login they themselves signed in with.
/// This removes it for real (POST /account/delete-login): signed out
/// everywhere, their email, phone and passkeys cleared, the address free to
/// be invited again. The restaurant's records stay the restaurant's.
struct AccountDeleteLoginView: View {
    let viewModel: AccountViewModel
    @Environment(SessionStore.self) private var sessionStore
    @State private var confirming = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Delete my login") {
                        GlowBadge(systemImage: "person.crop.circle.badge.xmark", size: 64)
                    } subtitle: {
                        Text(viewModel.summary?.account.email ?? "Your sign-in")
                    }

                    AccountSection(kicker: "What happens") {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(Self.whatHappens(restaurantName: viewModel.summary?.profile.restaurantName), id: \.self) {
                                bullet($0)
                            }
                        }
                        .padding(.vertical, 9)
                    }

                    if let error = viewModel.deleteLoginError {
                        Text(error).font(.cavnar(.body)).foregroundStyle(Color.cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    Button(role: .destructive) {
                        confirming = true
                    } label: {
                        Group {
                            if viewModel.isDeletingLogin {
                                CavnarShimmerText(text: "Deleting\u{2026}")
                            } else {
                                Text("Delete my login")
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isDeletingLogin))
                    .disabled(viewModel.isDeletingLogin)
                    .confirmationDialog("Delete your login?", isPresented: $confirming, titleVisibility: .visible) {
                        Button("Delete my login", role: .destructive) {
                            Task {
                                if await viewModel.deleteOwnLogin() {
                                    Haptic.success()
                                    await sessionStore.logout()
                                } else {
                                    Haptic.error()
                                }
                            }
                        }
                        Button("Cancel", role: .cancel) {}
                    } message: {
                        Text("This can't be undone. It ends at every location you sign in to, your staff PIN included, and you won't be able to sign in again unless an owner invites you.")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Delete My Login")
        }
    }

    /// What deleting the login does, as the server does it
    /// (auth.delete_own_login; re-audit 10/8/26, #5): everywhere the login
    /// signs in, not only this location.
    static func whatHappens(restaurantName: String?) -> [String] {
        [
            "You're signed out on every device, right away.",
            "It ends at every location you sign in to — \(restaurantName ?? "this one") and any other — your staff PIN included.",
            "Your email, phone, passkeys, two-factor and remembered devices come off the login.",
            "Each restaurant's own records — schedules, ratings, notes — stay theirs.",
            "An owner can invite you again any time.",
        ]
    }

    private func bullet(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Circle().fill(Color.cavnarEmber).frame(width: 5, height: 5)
            Text(text).font(.cavnar(.body)).foregroundStyle(Color.cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}


// MARK: - Pause

/// Self-serve pause, above the cancel request. GET /mobile/api/account/pause
/// says whether this login may pause and whether it already is; POST pauses
/// for a fixed number of days; /resume ends it early. Stripe collection
/// stops with behaviour "void" — nothing invoiced, nothing accrued.
struct AccountPauseSection: View {
    private struct Status: Decodable {
        let ok: Bool
        let paused: Bool?
        let pausedUntil: String?
        let days: [Int]?
        let canPause: Bool?
        /// A hold only Cavnar AI can lift (a past-due or admin pause): no
        /// Resume, and the server's sentence for why (re-audit M3).
        var locked: Bool? = nil
        var canResume: Bool? = nil
        var lockMessage: String? = nil
        enum CodingKeys: String, CodingKey {
            case ok, paused, days, locked
            case pausedUntil = "paused_until"
            case canPause = "can_pause"
            case canResume = "can_resume"
            case lockMessage = "lock_message"
        }
    }
    private struct PauseBody: Encodable { let days: Int }
    private typealias OK = APIClient.OKResponse

    @State private var status: Status?
    @State private var days = 30
    @State private var busy = false
    @State private var failure: String?
    @State private var confirming = false

    var body: some View {
        Group {
            if let st = status, st.canPause == true {
                AccountSection(kicker: "Need a break?") {
                    if st.paused == true {
                        let until = st.pausedUntil.map { "Billing and briefs are paused. Resumes \(Self.mdy($0))." }
                            ?? "Billing and briefs are paused."
                        if st.locked == true {
                            // Cavnar AI's hold: its reason, and no Resume —
                            // the route refuses one (re-audit M3).
                            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                                Text("Paused").cavnarText(.body, color: .cavnarInk)
                                Text(st.lockMessage ?? "This pause is held by Cavnar AI \u{2014} contact will@cavnar.ai to lift it.")
                                    .cavnarText(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .padding(.vertical, 9)
                        } else if st.canResume != false {
                            AccountActionRow(
                                label: "Resume now",
                                detail: until,
                                symbol: "play.fill", busy: busy, showsDivider: false
                            ) { Task { await act("/mobile/api/account/resume", body: PauseBody(days: 0)) } }
                        } else {
                            Text(until).cavnarText(.secondary).padding(.vertical, 9)
                        }
                    } else {
                        AccountKVRow(label: "Pause for") {
                            Picker("Pause length", selection: $days) {
                                ForEach(st.days ?? [14, 30, 60], id: \.self) { d in
                                    Text(d == 14 ? "2 weeks" : "\(d) days").tag(d)
                                }
                            }
                            .pickerStyle(.menu).tint(Color.cavnarEmber)
                        }
                        AccountActionRow(
                            label: "Pause the subscription",
                            detail: "Billing stops, nothing is invoiced, and it resumes on its own. Your data keeps flowing, so the day you come back the brief is current.",
                            symbol: "pause.fill", busy: busy, showsDivider: false
                        ) { confirming = true }
                    }
                    if let failure {
                        Text(failure).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                            .padding(.top, 6)
                    }
                }
                .confirmationDialog("Pause for \(days) days?", isPresented: $confirming, titleVisibility: .visible) {
                    Button("Pause") { Task { await act("/mobile/api/account/pause", body: PauseBody(days: days)) } }
                    Button("Cancel", role: .cancel) {}
                } message: {
                    Text("Billing stops, the briefs stop, and everything resumes on its own. You can resume sooner any time.")
                }
            }
        }
        .task { await load() }
    }

    private func load() async {
        status = try? await APIClient.shared.send("/mobile/api/account/pause", hapticOnError: false)
    }

    private func act(_ path: String, body: PauseBody) async {
        busy = true; failure = nil
        defer { busy = false }
        do {
            let r: OK = try await APIClient.shared.send(path, method: .post, body: body)
            if r.ok { Haptic.success() } else { failure = r.error ?? "That didn\u{2019}t go through." }
        } catch let e as APIClient.APIError { failure = e.message }
        catch { failure = "That didn\u{2019}t go through." }
        await load()
    }

    /// 2026-10-20 → 10/20/26, the product's date shape.
    private static func mdy(_ iso: String) -> String {
        let p = iso.prefix(10).split(separator: "-")
        guard p.count == 3, let y = Int(p[0]), let m = Int(p[1]), let d = Int(p[2]) else { return iso }
        return "\(m)/\(d)/\(String(format: "%02d", y % 100))"
    }
}
