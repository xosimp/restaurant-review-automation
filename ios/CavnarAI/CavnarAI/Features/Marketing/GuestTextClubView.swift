import SwiftUI

private enum CampaignField: Hashable, CaseIterable {
    case topic, draftMessage
}

struct GuestTextClubView: View {
    @State private var viewModel = GuestTextClubViewModel()
    @State private var showingAddContact = false
    @State private var copied = false
    @FocusState private var focusedField: CampaignField?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if let joinURL = viewModel.joinURL {
                    joinLinkCard(joinURL)
                }
                campaignCard
                contactsCard
            }
            .padding(20)
        }
        .background(Color.cavnarPaper)
        .navigationTitle("Guest Text Club")
        .toolbar { cavnarTitleToolbar("Guest Text Club") }
        .keyboardNavToolbar($focusedField)
        .task {
            await viewModel.load()
            await viewModel.loadJoinLink()
        }
        .sheet(isPresented: $showingAddContact) {
            AddGuestContactSheet(viewModel: viewModel)
        }
    }

    private func joinLinkCard(_ url: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Guest join link").font(.cavnarBody(14, weight: 700)).foregroundStyle(Color.cavnarInk3)
            Text("Guests join by scanning or tapping this themselves — that's the only way anyone becomes text-eligible.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 10) {
                Text(url)
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk)
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
                }
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
                            .font(.cavnarBody(14.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        ShareLink(item: Image(uiImage: qr),
                                  preview: SharePreview("Guest text club QR", image: Image(uiImage: qr))) {
                            Text("Share QR code")
                                .font(.cavnarBody(14, weight: 600))
                                .foregroundStyle(Color.cavnarEmber)
                        }
                    }
                    Spacer(minLength: 0)
                }
            }

            if let hint = viewModel.receiptHint {
                DisclosureGroup("Add this to your receipts") {
                    Text(hint)
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 6)
                }
                .font(.cavnarBody(14, weight: 600))
                .tint(Color.cavnarEmber)
            }
        }
        .cavnarCard()
    }

    private var campaignCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Send a campaign").font(.cavnarBody(14.5, weight: 700)).foregroundStyle(Color.cavnarInk)

            // guest_marketing.CAMPAIGN_PROMPTS. "Promo" used to sit here and
            // matched nothing on the backend, so it quietly became "general".
            CavnarSegmentedControl(
                selection: $viewModel.campaignType,
                options: GuestTextClubViewModel.campaignTypes
            ) { type in
                switch type {
                case "win_back": return "Win-back"
                case "event": return "Event"
                case "loyalty": return "Loyalty"
                default: return "General"
                }
            }

            (Text("Goes to your ")
                + Text("\(viewModel.textableCount)").font(.cavnarNumber(14, weight: 700))
                + Text(" text-eligible guest\(viewModel.textableCount == 1 ? "" : "s"), between 8:00 AM and 9:00 PM."))
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)

            TextField("Topic (optional)", text: $viewModel.campaignTopic)
                .cavnarTextFieldStyle()
                .focused($focusedField, equals: .topic)

            Button {
                Task { await viewModel.draftCampaign() }
            } label: {
                if viewModel.isDrafting {
                    CavnarShimmerText(text: "Drafting…")
                } else {
                    Text("Draft message")
                }
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(viewModel.isDrafting)

            if !viewModel.draftMessage.isEmpty {
                TextEditor(text: $viewModel.draftMessage)
                    .font(.cavnarBody(14.5))
                    .frame(minHeight: 80)
                    .padding(8)
                    .background(Color.cavnarPaper2)
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                    .focused($focusedField, equals: .draftMessage)

                Button {
                    Task { await viewModel.sendCampaign() }
                } label: {
                    if viewModel.isSending {
                        CavnarShimmerText(text: "Sending…")
                    } else {
                        Text("Send to consented guests")
                    }
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .disabled(viewModel.isSending)

                if viewModel.didSend {
                    // "Posted" — plays once on the real send, then clears
                    // itself so the form is ready for the next campaign.
                    CavnarInlinePosted(label: viewModel.sentCount.map { "Sent to \($0)" } ?? "Campaign sent") {
                        viewModel.didSend = false
                    }
                    .padding(.top, 6)
                }
            }

            if let error = viewModel.campaignError {
                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
            }
        }
        .cavnarCard()
    }

    private var contactsCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    (Text("Guest contacts (") + Text("\(viewModel.contacts.count)").font(.cavnarNumber(14.5, weight: 700)) + Text(")"))
                        .font(.cavnarBody(14.5, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                    if !viewModel.contacts.isEmpty {
                        (Text("\(viewModel.textableCount)").font(.cavnarNumber(14, weight: 700))
                            + Text(" text-eligible"))
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                Spacer()
                Button {
                    showingAddContact = true
                } label: {
                    Image(systemName: "plus.circle")
                }
            }
            if viewModel.isLoading {
                CavnarWorkingLine().padding(.vertical, 8)
            } else if let error = viewModel.errorMessage {
                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
            } else if viewModel.contacts.isEmpty {
                Text("Nobody has joined yet. Share the QR code above where guests can scan it.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ForEach(viewModel.contacts) { contact in
                    contactRow(contact)
                }
            }
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
                    .font(.cavnarBody(14.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Text(contact.phone).font(.cavnarNumber(14)).foregroundStyle(Color.cavnarInk3)
                Text(contact.statusLabel)
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(statusColor(contact.status))
                if let visit = contact.lastVisit, !visit.isEmpty {
                    Text("Last visit \(shortDate(visit))")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 10) {
                // Starts the post-visit review-request countdown. The route
                // existed; nothing in the app could call it.
                Button {
                    Task { await viewModel.markVisit(contact) }
                } label: {
                    Text("Mark visit")
                        .font(.cavnarBody(14, weight: 600))
                        .foregroundStyle(Color.cavnarEmber)
                }
                Button {
                    Haptic.selection()
                    Task { await viewModel.deleteContact(contact) }
                } label: {
                    Image(systemName: "trash").foregroundStyle(Color.cavnarEmber)
                }
            }
        }
        .padding(.vertical, 6)
    }

    private func statusColor(_ status: GuestContact.Status) -> Color {
        switch status {
        case .textable: return .cavnarGreen
        case .unsubscribed: return .cavnarInk3
        case .noConsent: return .cavnarEmber2
        }
    }

    /// last_visit is stored in the restaurant's own local time, not UTC (see
    /// guest_marketing.py), so it is read as a wall clock rather than being
    /// reinterpreted against the phone's timezone and shifted.
    private func shortDate(_ iso: String) -> String {
        let parser = DateFormatter()
        parser.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        parser.timeZone = TimeZone(identifier: "UTC")
        guard let date = parser.date(from: String(iso.prefix(19))) else { return iso }
        let out = DateFormatter()
        out.dateFormat = "MMM d"
        out.timeZone = TimeZone(identifier: "UTC")
        return out.string(from: date)
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

                    // Plain full-width buttons, not CavnarFormButtonPair —
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
