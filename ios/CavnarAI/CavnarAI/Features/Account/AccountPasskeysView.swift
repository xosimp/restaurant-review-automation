import SwiftUI
import UIKit
import Observation

/// Account → Security → Passkeys (iOS parity #57): this login's passkeys —
/// the web's list, on the bearer twins (/mobile/api/passkeys...). Adding one
/// asks for the password first (or a sign-in moments ago), so a phone left
/// unlocked can't plant a way back in; removing one is confirmed. A passkey
/// made here signs in on dashboard.cavnar.ai too, and the reverse.
struct AccountPasskeysView: View {
    @State private var model = AccountPasskeysModel()
    @State private var showingAdd = false
    @State private var pendingRemove: PasskeyRow?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Passkeys") {
                        GlowBadge(systemImage: "person.badge.key", size: 64)
                    } subtitle: {
                        Text(model.rows.isEmpty ? "Sign in with Face ID instead of a password"
                                                : "\(model.rows.count) on this login")
                    }

                    if model.isLoading && !model.loaded {
                        CavnarSkeletonBar(height: 3).padding(.vertical, 10)
                            .accessibilityLabel("Loading your passkeys")
                    } else {
                        AccountSection(kicker: "On this login") {
                            if let loadError = model.loadError, model.rows.isEmpty {
                                // A list that didn't load is never "None yet"
                                // (re-audit 10/8/26, #12).
                                VStack(alignment: .leading, spacing: 10) {
                                    Text(loadError).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                                        .fixedSize(horizontal: false, vertical: true)
                                    Button {
                                        Haptic.light()
                                        Task { await model.load() }
                                    } label: { Text("Try again").frame(maxWidth: .infinity) }
                                        .buttonStyle(CavnarSecondaryButtonStyle())
                                }
                                .padding(.vertical, 9)
                            } else if model.rows.isEmpty {
                                Text("None yet. A passkey lives in your iCloud Keychain and signs you in with Face ID — here and on dashboard.cavnar.ai.")
                                    .font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .padding(.vertical, 9)
                            }
                            ForEach(Array(model.rows.enumerated()), id: \.element.id) { i, row in
                                HStack(alignment: .center, spacing: 12) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(row.name).font(.cavnarBody(CavnarType.body, weight: 700)).foregroundStyle(Color.cavnarInk)
                                        HomeMixedText.make(row.detail, size: CavnarType.secondary, weight: 500, color: .cavnarInk3)
                                    }
                                    Spacer(minLength: 8)
                                    if model.busyId == row.id {
                                        CavnarShimmerLine(color: .cavnarRed).frame(width: 28)
                                    } else {
                                        AccountActionChip(symbol: "xmark", tone: .cavnarRed,
                                                          accessibilityLabel: "Remove \(row.name)") {
                                            pendingRemove = row
                                        }
                                    }
                                }
                                .padding(.vertical, 9)
                                .contextMenu {
                                    Button(role: .destructive) { pendingRemove = row } label: {
                                        Label("Remove", systemImage: "trash")
                                    }
                                }
                                if i < model.rows.count - 1 { AccountRowDivider() }
                            }
                        }
                    }

                    if let error = model.error {
                        Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    Button {
                        Haptic.light()
                        showingAdd = true
                    } label: {
                        Group {
                            if model.adding { CavnarShimmerText(text: "Adding\u{2026}") } else { Text("Add a passkey") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: model.adding))
                    .disabled(model.adding)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Passkeys")
            .cavnarPostedOverlay(model.posted) { model.posted = nil }
            .task { await model.load() }
            .sheet(isPresented: $showingAdd) {
                PasskeyPasswordSheet { password in
                    showingAdd = false
                    Task { await model.add(password: password) }
                }
                .presentationDetents([.medium])
            }
            .confirmationDialog(
                pendingRemove.map { "Remove \($0.name)?" } ?? "",
                isPresented: Binding(get: { pendingRemove != nil }, set: { if !$0 { pendingRemove = nil } }),
                titleVisibility: .visible
            ) {
                Button("Remove passkey", role: .destructive) {
                    guard let row = pendingRemove else { return }
                    Task { await model.remove(row) }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("It stops signing you in here and on the web. Remove it from your Passwords app too, if you like.")
            }
        }
    }
}

/// The password step-up before a passkey is added.
private struct PasskeyPasswordSheet: View {
    var onContinue: (String) -> Void
    @State private var password = ""
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 18) {
                Text("Confirm it's you")
                    .font(.cavnarHeadline(CavnarType.section)).foregroundStyle(Color.cavnarInk)
                Text("Type your password, then save the passkey with Face ID.")
                    .font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2)
                SecureField("Password", text: $password)
                    .textContentType(.password)
                    .font(.cavnarBody(CavnarType.body, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .focused($focused)
                    .submitLabel(.continue)
                    .onSubmit { if !password.isEmpty { onContinue(password) } }
                    .padding(.vertical, 10)
                    .overlay(alignment: .bottom) {
                        Rectangle().fill(focused ? Color.cavnarEmber : Color.cavnarPaper3).frame(height: focused ? 1.5 : 1)
                    }
                Button {
                    onContinue(password)
                } label: {
                    Text("Continue").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: password.isEmpty))
                .disabled(password.isEmpty)
                Spacer(minLength: 0)
            }
            .padding(20)
            .accountSheetChrome("Add a Passkey")
            .onAppear { focused = true }
        }
    }
}

/// One passkey on this login (GET /passkeys row; dates are the server's M/D/YY).
struct PasskeyRow: Decodable, Identifiable, Equatable {
    let id: Int
    let name: String
    let created: String?
    let lastUsed: String?
    let backedUp: Bool

    enum CodingKeys: String, CodingKey { case id, name, created; case lastUsed = "last_used"; case backedUp = "backed_up" }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.setupInt(.id) ?? 0
        name = c.setupText(.name) ?? "Passkey"
        created = c.setupText(.created)
        lastUsed = c.setupText(.lastUsed)
        backedUp = c.setupBool(.backedUp) ?? false
    }

    /// "Added 10/7/26 · last used 10/8/26 · synced with iCloud".
    var detail: String {
        var parts: [String] = []
        if let created { parts.append("Added " + created) }
        parts.append(lastUsed.map { "last used " + $0 } ?? "not used yet")
        if backedUp { parts.append("synced") }
        return parts.joined(separator: " \u{00B7} ")
    }
}

@Observable
@MainActor
final class AccountPasskeysModel {
    var rows: [PasskeyRow] = []
    var loaded = false
    var isLoading = false
    var adding = false
    var busyId: Int?
    var error: String?
    /// The list couldn't be read — said, with Try again, never "None yet".
    var loadError: String?
    var posted: String?

    private struct ListResponse: Decodable { let ok: Bool; let passkeys: [PasskeyRow]?; let error: String? }
    private struct OptionsResponse: Decodable { let ok: Bool; let options: PasskeyRegistrationOptions?; let error: String? }
    private struct PasswordBody: Encodable { let password: String }
    /// Every key the register route reads: the credential, and the device
    /// it was made on, which names it (mobile_api.mobile_passkeys_register —
    /// an iPad's passkey was saved as "iPhone"; re-audit 10/8/26, #11).
    struct RegisterBody: Encodable, Equatable {
        let credential: PasskeyCredentialJSON
        let device: String
    }

    /// "iPhone", "iPad" or "Mac" — the words the server names a passkey by.
    static func deviceName(idiom: UIUserInterfaceIdiom = UIDevice.current.userInterfaceIdiom,
                           onMac: Bool = ProcessInfo.processInfo.isiOSAppOnMac) -> String {
        if onMac || idiom == .mac { return "Mac" }
        return idiom == .pad ? "iPad" : "iPhone"
    }

    private let client: APIClient
    private let coordinator = PasskeyCoordinator()
    init(client: APIClient = .shared) { self.client = client }

    func load() async {
        isLoading = true
        defer { isLoading = false; loaded = true }
        do {
            let r: ListResponse = try await client.send("/mobile/api/passkeys", hapticOnError: false)
            if r.ok {
                rows = r.passkeys ?? []
                loadError = nil
            } else {
                loadError = r.error ?? "Your passkeys couldn\u{2019}t be loaded."
            }
        } catch is CancellationError {
        } catch let e as APIClient.APIError {
            loadError = e.message
        } catch {
            loadError = "Your passkeys couldn\u{2019}t be loaded."
        }
    }

    /// The step-up, the system's "Save a passkey?", then the save.
    func add(password: String) async {
        adding = true
        error = nil
        defer { adding = false }
        do {
            let o: OptionsResponse = try await client.send("/mobile/api/passkeys/options", method: .post,
                                                           body: PasswordBody(password: password), retryTransient: false)
            guard o.ok, let options = o.options else { error = o.error ?? "Couldn\u{2019}t start that."; return }
            let credential = try await coordinator.register(options)
            let r: APIClient.OKResponse = try await client.send("/mobile/api/passkeys", method: .post,
                                                                body: RegisterBody(credential: credential, device: Self.deviceName()),
                                                                retryTransient: false)
            guard r.ok else { error = r.error ?? "That passkey couldn\u{2019}t be saved."; return }
            Haptic.success()
            posted = "Passkey added"
            await load()
        } catch let e as PasskeyError {
            error = e.message
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "That passkey couldn\u{2019}t be saved."
        }
    }

    func remove(_ row: PasskeyRow) async {
        busyId = row.id
        error = nil
        defer { busyId = nil }
        do {
            let r: APIClient.OKResponse = try await client.send("/mobile/api/passkeys/\(row.id)/remove", method: .post,
                                                                retryTransient: false)
            guard r.ok else { error = r.error ?? "Couldn\u{2019}t remove it."; return }
            Haptic.success()
            posted = "Passkey removed"
            await load()
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t remove it."
        }
    }
}
