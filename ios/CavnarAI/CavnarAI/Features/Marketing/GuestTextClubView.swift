import SwiftUI

private enum ClubField: Hashable, CaseIterable {
    case search
}

/// The Guest Text Club since it folded into Campaigns (readability round
/// 10/8/26 #57): how guests join — the link, the QR code to print, the
/// receipt line — and who has, searchable and paged. Writing and sending a
/// text or an email, what went out and the consent ledger are on Marketing
/// → Campaigns, once; this screen used to repeat each of them.
struct GuestTextClubView: View {
    @State private var viewModel = GuestTextClubViewModel()
    @State private var showingAddContact = false
    @State private var copied = false
    /// The guest whose trash button was tapped, awaiting confirmation.
    @State private var contactToDelete: GuestContact?
    /// Finds a guest by name or number.
    @State private var search = ""
    /// How many matching guests are on screen; "Show more" adds a page.
    @State private var shown = GuestTextClubView.pageSize
    /// The guest list is folded until asked for — the join link, the QR
    /// code and Add guest are what the phone is for; the full list is the
    /// web's (re-audit 10/8/26 W7).
    @State private var showingGuests = false
    @FocusState private var focusedField: ClubField?

    static let pageSize = 25

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: CavnarSpace.l) {
                if let joinURL = viewModel.joinURL {
                    joinLinkCard(joinURL)
                }
                contactsCard
            }
            .padding(CavnarSpace.gutter)
        }
        .background(Color.cavnarPaper)
        .navigationTitle("Guest Text Club")
        .toolbar { cavnarTitleToolbar("Guest Text Club") }
        .keyboardNavToolbar($focusedField)
        .task { await viewModel.loadClub() }
        .cavnarEmberRefreshable { await viewModel.loadClub() }
        .sheet(isPresented: $showingAddContact) {
            AddGuestContactSheet(viewModel: viewModel)
        }
        // One tap used to delete a guest outright — and with them the
        // record of their consent or their STOP (CLIENT-34). It asks first,
        // and says what goes with it.
        .confirmationDialog(
            "Delete this guest?",
            isPresented: Binding(get: { contactToDelete != nil }, set: { if !$0 { contactToDelete = nil } }),
            titleVisibility: .visible,
            presenting: contactToDelete
        ) { contact in
            Button("Delete \(contact.name?.isEmpty == false ? contact.name! : "guest")", role: .destructive) {
                Task { await viewModel.deleteContact(contact) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { contact in
            Text(contact.status == .unsubscribed
                 ? "This also deletes the record that they texted STOP. If their number is added again, nothing will show they opted out."
                 : "This also deletes the record of their consent to be texted.")
        }
        .onChange(of: search) { _, _ in shown = Self.pageSize }
    }

    private func joinLinkCard(_ url: String) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Guest join link")
            Text("Guests join by scanning or tapping this themselves — that's the only way anyone becomes text-eligible.")
                .cavnarText(.body)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 10) {
                Text(url)
                    .cavnarText(.secondary, color: .cavnarInk)
                    .textSelection(.enabled)
                    .lineLimit(2)
                Spacer(minLength: 8)
                Button {
                    UIPasteboard.general.string = url
                    Haptic.success()
                    copied = true
                } label: {
                    Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                        .labelStyle(.iconOnly)
                        .foregroundStyle(Color.cavnarEmber)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityLabel(copied ? "Copied" : "Copy the join link")
            }

            // A QR on a screen helps nobody — the whole point is that it gets
            // printed, so it's rendered here at print size and handed to the
            // share sheet. This was web-only.
            if let qr = viewModel.joinQRCode() {
                HStack(spacing: 14) {
                    Image(uiImage: qr)
                        .interpolation(.none)
                        .resizable()
                        .frame(width: 84, height: 84)
                        .padding(6)
                        .background(Color.white)
                        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Print this where guests can scan it")
                            .cavnarText(.label)
                        ShareLink(item: Image(uiImage: qr),
                                  preview: SharePreview("Guest text club QR", image: Image(uiImage: qr))) {
                            Text("Share QR code")
                                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                                .frame(minHeight: 44)
                        }
                    }
                    Spacer(minLength: 0)
                }
            }

            if let hint = viewModel.receiptHint {
                DisclosureGroup("Add this to your receipts") {
                    Text(hint)
                        .cavnarText(.body)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 6)
                }
                .font(.cavnar(.label))
                .tint(Color.cavnarEmber)
            }
        }
        .cavnarCard()
    }

    /// Guests whose name or number matches the search.
    private var matching: [GuestContact] {
        let q = search.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return viewModel.contacts }
        let digits = q.filter(\.isNumber)
        return viewModel.contacts.filter { c in
            (c.name ?? "").lowercased().contains(q)
                || (!digits.isEmpty && c.phone.filter(\.isNumber).contains(digits))
        }
    }

    private var contactsCard: some View {
        let list = matching
        let page = Array(list.prefix(shown))
        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    HomeMixedText.make("Guests (\(viewModel.contacts.count))", role: .label)
                    if !viewModel.contacts.isEmpty {
                        HomeMixedText.make("\(viewModel.textableCount) text-eligible", role: .secondary)
                    }
                }
                Spacer()
                Button {
                    showingAddContact = true
                } label: {
                    Image(systemName: "plus.circle")
                        .font(.cavnar(.lead))
                        .foregroundStyle(Color.cavnarEmber)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Add a guest")
            }
            if !viewModel.contacts.isEmpty {
                Button {
                    Haptic.light()
                    withAnimation(.cavnarEase(0.22)) { showingGuests.toggle() }
                } label: {
                    HStack(spacing: CavnarSpace.xxs + 2) {
                        Text(showingGuests ? "Hide the guests" : "Show the guests")
                            .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        Image(systemName: "chevron.down")
                            .font(.cavnar(.caption))
                            .rotationEffect(.degrees(showingGuests ? 180 : 0))
                            .accessibilityHidden(true)
                        Spacer(minLength: 0)
                    }
                    .foregroundStyle(Color.cavnarEmber2)
                    .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityValue(showingGuests ? "Expanded" : "Collapsed")
            }
            if showingGuests && viewModel.contacts.count > Self.pageSize {
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass").foregroundStyle(Color.cavnarInk3)
                    TextField("Search guests", text: $search)
                        .font(.cavnar(.body))
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .focused($focusedField, equals: .search)
                    if !search.isEmpty {
                        Button {
                            Haptic.light()
                            search = ""
                        } label: {
                            Image(systemName: "xmark.circle.fill").foregroundStyle(Color.cavnarInk3)
                                .cavnarHitTarget()
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Clear search")
                    }
                }
                .padding(.horizontal, 12)
                .background(Color.cavnarPaper2)
                .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .clipShape(Capsule())
            }
            // A failed action (a delete the server refused) is said above the
            // list rather than replacing it.
            if let error = viewModel.errorMessage {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            }
            if viewModel.isLoading {
                CavnarWorkingLine().padding(.vertical, 8)
            } else if viewModel.contacts.isEmpty && viewModel.errorMessage != nil {
                EmptyView()
            } else if viewModel.contacts.isEmpty {
                Text("Nobody has joined yet. Share the QR code above where guests can scan it.")
                    .cavnarText(.body)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !showingGuests {
                EmptyView()
            } else if list.isEmpty {
                Text("No guest matches \u{201C}\(search)\u{201D}.")
                    .cavnarText(.body)
            } else {
                // Paged: a text club is thousands of guests and
                // get_guest_contacts applies no LIMIT — 25 at a time, lazily.
                LazyVStack(alignment: .leading, spacing: CavnarSpace.s) {
                    ForEach(page) { contact in
                        contactRow(contact)
                    }
                }
                if list.count > page.count {
                    Button {
                        Haptic.light()
                        shown += Self.pageSize
                    } label: {
                        HStack(spacing: CavnarSpace.xxs + 2) {
                            HomeMixedText.make("Show \(min(Self.pageSize, list.count - page.count)) more of \(list.count)",
                                               role: .label, color: .cavnarEmber2, numberColor: .cavnarEmber2)
                            Image(systemName: "chevron.down")
                                .font(.cavnar(.caption))
                                .foregroundStyle(Color.cavnarEmber2)
                                .accessibilityHidden(true)
                            Spacer(minLength: 0)
                        }
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
            }
            CavnarWebLinkRow(title: "All guests", subtitle: "Search and tidy the whole list on the web",
                             path: "marketing/guests", actionLabel: "Open on the web")
        }
        .cavnarCard()
    }

    /// Three states, not one badge. A guest who consented and then replied
    /// STOP used to render exactly like one who consented and stayed — the app
    /// never decoded `unsubscribed` at all.
    private func contactRow(_ contact: GuestContact) -> some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 2) {
                Text(contact.name?.isEmpty == false ? contact.name! : "Guest")
                    .cavnarText(.label)
                Text(PhoneFormat.display(contact.phone))
                    .font(.cavnarNumber(CavnarType.secondary, weight: 500))
                    .foregroundStyle(Color.cavnarInk2)
                Text(contact.statusLabel)
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(statusColor(contact.status))
                if let visit = contact.lastVisit, !visit.isEmpty {
                    HomeMixedText.make("Last visit \(shortDate(visit))", role: .caption)
                }
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 0) {
                // Starts the post-visit review-request countdown. The route
                // existed; nothing in the app could call it.
                // Rests for a moment after a tap, and says "Visit marked"
                // only once the server took it (re-audit 10/8/26 L2) — every
                // tap used to count a visit and buzz success either way.
                let marked = viewModel.visitMarked.contains(contact.id)
                Button {
                    Task { await viewModel.markVisit(contact) }
                } label: {
                    Text(marked ? "Visit marked" : "Mark visit")
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(marked ? Color.cavnarGreen : Color.cavnarEmber2)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .disabled(marked || viewModel.markingVisit.contains(contact.id))
                Button {
                    Haptic.selection()
                    contactToDelete = contact
                } label: {
                    Image(systemName: "trash")
                        .foregroundStyle(Color.cavnarInk2)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Delete \(contact.name?.isEmpty == false ? contact.name! : "guest")")
            }
        }
        .padding(.vertical, 4)
    }

    private func statusColor(_ status: GuestContact.Status) -> Color {
        switch status {
        case .textable: return .cavnarGreen
        case .unsubscribed: return .cavnarInk2
        case .noConsent: return .cavnarEmber2
        }
    }

    /// last_visit is stored in the restaurant's own local time, not UTC (see
    /// guest_marketing.py), so its date is read as written rather than being
    /// reinterpreted against the phone's timezone and shifted. M/D/YY.
    private func shortDate(_ iso: String) -> String {
        CavnarDate.mdy(iso)
    }
}

private enum AddGuestContactField: Hashable, CaseIterable {
    case name, phone
}

private struct AddGuestContactSheet: View {
    let viewModel: GuestTextClubViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var phone = ""
    @State private var isAdding = false
    @State private var postedLabel: String?
    @FocusState private var focusedField: AddGuestContactField?

    private var canAdd: Bool { !isAdding && !name.isEmpty && !phone.isEmpty }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    CavnarFloatingField(
                        icon: "person", placeholder: "Guest name", text: $name, textContentType: .name,
                        focus: $focusedField, field: .name
                    )
                    CavnarFloatingField(
                        icon: "phone", placeholder: "Phone", text: $phone, keyboardType: .phonePad,
                        focus: $focusedField, field: .phone
                    )
                    // Adding a number is not consent (re-audit 10/8/26 L16).
                    Text("An added guest can\u{2019}t be texted until they join with your link or QR code themselves.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, -14)

                    // Plain full-width buttons, not a width-matched pair —
                    // see SendReviewRequestSheet's identical comment; same
                    // PreferenceKey width-matching bug, same fix.
                    VStack(spacing: 10) {
                        Button {
                            Task {
                                isAdding = true
                                let added = await viewModel.addContact(name: name, phone: phone)
                                isAdding = false
                                if added {
                                    Haptic.success()
                                    postedLabel = "Guest added"
                                }
                            }
                        } label: {
                            Group {
                                if isAdding {
                                    CavnarShimmerText(text: "Adding…", color: Color.cavnarInk)
                                } else {
                                    Text("Add guest")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canAdd))
                        .disabled(!canAdd)

                        Button {
                            dismiss()
                        } label: {
                            Text("Cancel").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                    .padding(.top, 6)
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Add Guest")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Add Guest") }
            .keyboardNavToolbar($focusedField)
            .cavnarPostedOverlay(postedLabel) { dismiss() }
        }
    }
}
