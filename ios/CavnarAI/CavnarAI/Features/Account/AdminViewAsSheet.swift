import SwiftUI

/// Account → Cavnar AI admin → View as a client (10/8/26): every restaurant
/// with an owner login, searchable, then a confirm that says what the view
/// is before it opens — the web console's View as client, on the phone.
struct AdminViewAsSheet: View {
    @Environment(SessionStore.self) private var session
    @Environment(\.dismiss) private var dismiss

    @State private var clients: [ViewAsClient] = []
    @State private var hours = 2
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var query = ""
    @State private var confirming: ViewAsClient?
    @State private var opening: Int?

    private var shown: [ViewAsClient] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return clients }
        return clients.filter {
            $0.name.lowercased().contains(q) || ($0.detail?.lowercased().contains(q) ?? false)
                || String($0.id) == q
        }
    }

    var body: some View {
        NavigationStack {
            List {
                if let errorMessage {
                    Text(errorMessage)
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarRed)
                }
                Section {
                    ForEach(shown) { client in
                        Button {
                            Haptic.selection()
                            confirming = client
                        } label: {
                            row(client)
                        }
                        .disabled(opening != nil)
                    }
                } header: {
                    Text("CLIENTS")
                        .font(.cavnarBody(13, weight: 700))
                        .tracking(1.4)
                        .foregroundStyle(Color.cavnarInk3)
                } footer: {
                    Text("Opens the app as their owner login for \(hours) hours. A banner stays up the whole time, "
                         + "and anything you change is recorded under your name.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            .scrollContentBackground(.hidden)
            .searchable(text: $query, prompt: "Search clients")
            .overlay {
                if isLoading { CavnarLoadingOrb() }
            }
            .overlay(alignment: .top) {
                if !isLoading && clients.isEmpty && errorMessage == nil {
                    CavnarEmptyHearth(title: "No clients yet",
                                      message: "A restaurant shows here once it has an owner login.")
                        .padding(.top, 40)
                }
            }
            .accountSheetChrome("View as a client")
            .task { await load() }
            .confirmationDialog(confirmTitle, isPresented: Binding(
                get: { confirming != nil }, set: { if !$0 { confirming = nil } }
            ), titleVisibility: .visible, presenting: confirming) { client in
                Button("Open \(client.name)") { open(client) }
                Button("Cancel", role: .cancel) {}
            } message: { _ in
                Text("You'll see the app exactly as their owner does, for \(hours) hours. "
                     + "Anything you change is recorded under your name.")
            }
        }
    }

    private var confirmTitle: String {
        "View as \(confirming?.name ?? "this client")?"
    }

    private func row(_ client: ViewAsClient) -> some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(client.name)
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk)
                if let detail = client.detail {
                    Text(detail)
                        .font(.cavnarBody(12.5, weight: 500))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer()
            if client.isDemo {
                Text("DEMO")
                    .font(.cavnarBody(11.5, weight: 700))
                    .tracking(0.8)
                    .foregroundStyle(Color.cavnarInk3)
            }
            if opening == client.id {
                ProgressView().tint(Color.cavnarEmber)
            } else {
                Image(systemName: "eye")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber)
            }
        }
        .frame(minHeight: 44)
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let r: ViewAsClientsResponse = try await APIClient.shared.send("/mobile/api/admin/clients",
                                                                           hapticOnError: false)
            clients = r.clients
            hours = r.hours ?? hours
            errorMessage = r.ok ? nil : (r.error ?? "Couldn't load your clients.")
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load your clients. Check your connection and try again."
        }
    }

    private func open(_ client: ViewAsClient) {
        opening = client.id
        errorMessage = nil
        Task {
            do {
                try await session.startViewAs(client)
                Haptic.success()
                dismiss()
            } catch let error as APIClient.APIError {
                errorMessage = error.message
            } catch {
                errorMessage = "Couldn't open \(client.name). Try again."
            }
            opening = nil
        }
    }
}

/// RootView's strip above every screen while an admin is viewing as a
/// client — the web's #view-as-banner (DESIGN_SYSTEM → Account banner):
/// amber ground, paper text, whose login this is, what a change does, when
/// it closes, and the way back. Under it, one line when a view has just
/// ended.
struct ViewAsBanner: View {
    @Environment(SessionStore.self) private var session
    @State private var stopping = false

    var body: some View {
        VStack(spacing: 0) {
            if let view = session.viewAs {
                HStack(spacing: 10) {
                    Image(systemName: "eye.fill")
                        .font(.system(size: 13, weight: .bold))
                    VStack(alignment: .leading, spacing: 1) {
                        Text("Viewing as \(view.restaurantName)")
                            .font(.cavnarBody(13.5, weight: 700))
                            .lineLimit(1)
                        Text(Self.detail(view))
                            .font(.cavnarBody(12, weight: 600))
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 6)
                    Button {
                        stopping = true
                        Task {
                            await session.stopViewAs()
                            stopping = false
                        }
                    } label: {
                        if stopping {
                            ProgressView().tint(Color.cavnarPaper)
                        } else {
                            Text("Stop")
                                .font(.cavnarBody(13.5, weight: 700))
                        }
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(Capsule().strokeBorder(Color.cavnarPaper.opacity(0.6), lineWidth: 1.2))
                    .disabled(stopping)
                    .accessibilityLabel("Stop viewing as \(view.restaurantName)")
                }
                .foregroundStyle(Color.cavnarPaper)
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity)
                .background(Color.cavnarAmber)
                .accessibilityElement(children: .contain)
                // Ends on its own at the server's time, whether or not a
                // request happens to be made then.
                .task(id: view.endsAt) {
                    let wait = view.endsAt.timeIntervalSinceNow
                    if wait > 0 { try? await Task.sleep(for: .seconds(wait)) }
                    guard !Task.isCancelled, session.viewAs?.endsAt == view.endsAt else { return }
                    await session.stopViewAs()
                }
            }
            if let notice = session.viewAsNotice {
                HStack(spacing: 8) {
                    Text(notice)
                        .font(.cavnarBody(12.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 6)
                    Button {
                        session.viewAsNotice = nil
                    } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(Color.cavnarInk3)
                            .frame(width: 44, height: 32)
                    }
                    .accessibilityLabel("Dismiss")
                }
                .padding(.leading, 16)
                .padding(.vertical, 4)
                .frame(maxWidth: .infinity)
                .background(Color.cavnarAmberBg)
                .task {
                    try? await Task.sleep(for: .seconds(8))
                    session.viewAsNotice = nil
                }
            }
        }
        .animation(.easeOut(duration: 0.25), value: session.viewAs)
        .animation(.easeOut(duration: 0.25), value: session.viewAsNotice)
    }

    static func detail(_ view: ViewAsSession) -> String {
        let change = view.readOnly ? "Read-only: nothing can be changed." : "Changes are recorded under your name."
        let time = view.endsAt.formatted(date: .omitted, time: .shortened)
        return "\(change) Ends at \(time)."
    }
}
