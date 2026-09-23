import SwiftUI

private enum CampaignField: Hashable, CaseIterable {
    case topic, link, draftMessage, newsletter
}

struct GuestTextClubView: View {
    @State private var viewModel = GuestTextClubViewModel()
    @State private var showingAddContact = false
    @State private var copied = false
    @State private var confirmingSend = false
    /// The guest whose trash button was tapped, awaiting confirmation.
    @State private var contactToDelete: GuestContact?
    @FocusState private var focusedField: CampaignField?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if let joinURL = viewModel.joinURL {
                    joinLinkCard(joinURL)
                }
                campaignCard
                newsletterCard
                historyCard
                ledgerCard
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
            await viewModel.loadSegments()
            await viewModel.loadHistory()
            await viewModel.loadNewsletter()
        }
        .onChange(of: viewModel.campaignType) { _, _ in viewModel.campaignTypeChanged() }
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
    }

    private func joinLinkCard(_ url: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Guest join link").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk3)
            Text("Guests join by scanning or tapping this themselves — that's the only way anyone becomes text-eligible.")
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 10) {
                Text(url)
                    .font(.cavnarBody(15))
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
                            .font(.cavnarBody(16, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        ShareLink(item: Image(uiImage: qr),
                                  preview: SharePreview("Guest text club QR", image: Image(uiImage: qr))) {
                            Text("Share QR code")
                                .font(.cavnarBody(15, weight: 600))
                                .foregroundStyle(Color.cavnarEmber)
                        }
                    }
                    Spacer(minLength: 0)
                }
            }

            if let hint = viewModel.receiptHint {
                DisclosureGroup("Add this to your receipts") {
                    Text(hint)
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 6)
                }
                .font(.cavnarBody(15, weight: 600))
                .tint(Color.cavnarEmber)
            }
        }
        .cavnarCard()
    }

    private var campaignCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Send a campaign").font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)

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

            if !viewModel.segments.isEmpty {
                Picker("Audience", selection: $viewModel.selectedSegment) {
                    ForEach(viewModel.segments) { segment in
                        Text("\(segment.label) (\(segment.count))").tag(segment.key)
                    }
                }
                .pickerStyle(.menu)
                .tint(Color.cavnarEmber)

                if let help = viewModel.selectedSegmentHelp {
                    Text(help)
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if viewModel.audienceUnknown {
                // Unknown is never shown as zero (CLIENT-9).
                HStack(spacing: 10) {
                    Text("Couldn't load who this goes to.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarRed)
                    Button("Retry") { Task { await viewModel.loadSegments() } }
                        .font(.cavnarBody(15, weight: 600))
                }
            } else {
                (Text("Goes to ")
                    + Text("\(viewModel.selectedSegmentCount)").font(.cavnarNumber(15, weight: 700))
                    + Text(" guest\(viewModel.selectedSegmentCount == 1 ? "" : "s"), between 8:00 AM and 9:00 PM. Nobody gets two campaigns inside three days."))
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }

            TextField("Topic (optional)", text: $viewModel.campaignTopic)
                .cavnarTextFieldStyle()
                .focused($focusedField, equals: .topic)

            // draft_campaign_message's own prompt forbids links, so a text
            // could never carry one — meaning a text club could ask guests to
            // come back and never learn whether one did. A short link is
            // appended on send and the taps are counted.
            TextField("Link to track (optional)", text: $viewModel.linkURL)
                .cavnarTextFieldStyle()
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .focused($focusedField, equals: .link)

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
                    .font(.cavnarBody(16))
                    .frame(minHeight: 80)
                    .padding(8)
                    .background(Color.cavnarPaper2)
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                    .focused($focusedField, equals: .draftMessage)

                // A text blast can't be taken back, so it is confirmed with
                // the audience and the count first, like the web's
                // "Send this text to …?" (CLIENT-9).
                Button {
                    confirmingSend = true
                } label: {
                    if viewModel.isSending {
                        CavnarShimmerText(text: "Sending…")
                    } else {
                        Text("Send to consented guests")
                    }
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .disabled(viewModel.isSending || viewModel.audienceUnknown)
                .confirmationDialog(sendConfirmationTitle, isPresented: $confirmingSend, titleVisibility: .visible) {
                    Button("Send to \(viewModel.selectedSegmentCount) guest\(viewModel.selectedSegmentCount == 1 ? "" : "s")") {
                        Task { await viewModel.sendCampaign() }
                    }
                    Button("Cancel", role: .cancel) {}
                } message: {
                    Text("Texts go out between 8:00 AM and 9:00 PM. A sent text can't be recalled.")
                }

                if viewModel.didSend {
                    // "Posted" — plays once on the real send, then clears
                    // itself so the form is ready for the next campaign.
                    CavnarInlinePosted(label: viewModel.queuedCount.map { "Sending to \($0)" }
                                       ?? viewModel.sentCount.map { "Sent to \($0)" } ?? "Campaign sent") {
                        viewModel.didSend = false
                    }
                    .padding(.top, 6)
                }
            }

            if let error = viewModel.campaignError {
                Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
            }
        }
        .cavnarCard()
    }

    /// `weekly_email` has generated newsletters since this product existed
    /// with no list to send them to and no way to send one.
    private var sendConfirmationTitle: String {
        let count = viewModel.selectedSegmentCount
        let label = viewModel.segments.first { $0.key == viewModel.selectedSegment }?.label ?? "your guests"
        return "Text \(label) — \(count) guest\(count == 1 ? "" : "s")?"
    }

    private var newsletterCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Email newsletter").font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                Spacer()
                if !viewModel.newsletterLoadFailed {
                    (Text("\(viewModel.subscriberCount)").font(.cavnarNumber(15, weight: 700))
                        + Text(" subscribed"))
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }

            if viewModel.newsletterLoadFailed {
                // A failed load is not an empty list (CLIENT-58).
                HStack(spacing: 10) {
                    Text(viewModel.newsletterError ?? "Couldn't load your email list.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarRed)
                    Button("Retry") { Task { await viewModel.loadNewsletter() } }
                        .font(.cavnarBody(15, weight: 600))
                }
            } else if viewModel.subscriberCount == 0 {
                Text("Nobody has opted in to email yet. The join page asks for an address, separately from the text club — a guest can say yes to one and not the other.")
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("Paste in a Weekly email you generated on the Content tab. The subject line block gets stripped out — guests never see the scaffolding.")
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)

                TextField("Subject (optional)", text: $viewModel.newsletterSubject)
                    .cavnarTextFieldStyle()

                TextEditor(text: $viewModel.newsletterBody)
                    .font(.cavnarBody(16))
                    .scrollContentBackground(.hidden)
                    .frame(minHeight: 110)
                    .padding(8)
                    .background(Color.cavnarPaper2)
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                    .focused($focusedField, equals: .newsletter)

                Button {
                    Task { await viewModel.sendNewsletter() }
                } label: {
                    if viewModel.isSendingNewsletter {
                        CavnarShimmerText(text: "Sending…")
                    } else {
                        Text("Send to subscribers").frame(maxWidth: .infinity)
                    }
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .disabled(viewModel.isSendingNewsletter || viewModel.newsletterBody.isEmpty)

                if let result = viewModel.newsletterResult {
                    Text(result).font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarGreen)
                }
                if let error = viewModel.newsletterError {
                    Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                }
            }
        }
        .cavnarCard()
    }

    /// What has already gone out. This was recorded from the beginning and
    /// displayed nowhere, so "did we already text about the wine dinner?"
    /// had no answer.
    @ViewBuilder
    private var historyCard: some View {
        if !viewModel.campaigns.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Text("Campaigns sent").font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                ForEach(viewModel.campaigns) { campaign in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(campaign.whenLabel)
                                .font(.cavnarBody(15, weight: 700))
                                .foregroundStyle(Color.cavnarEmber)
                            Spacer()
                            (Text("\(campaign.sentCount)").font(.cavnarNumber(15, weight: 700))
                                + Text(" sent")
                                + Text(campaign.clicks > 0 ? " · " : "")
                                + (campaign.clicks > 0
                                   ? Text("\(campaign.clicks)").font(.cavnarNumber(15, weight: 700)) + Text(" taps")
                                   : Text("")))
                                .font(.cavnarBody(15))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        if let label = campaign.segmentLabel {
                            Text(label).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
                        }
                        Text(campaign.message)
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk)
                            .lineLimit(3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.cavnarPaper2)
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                }
            }
            .cavnarCard()
        }
    }

    /// The rules this list runs under, stated rather than assumed.
    @ViewBuilder
    private var ledgerCard: some View {
        if let ledger = viewModel.ledger {
            VStack(alignment: .leading, spacing: 10) {
                Text("Consent & sending").font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                HStack(spacing: 0) {
                    ledgerTile("\(ledger.textable)", "Text-eligible", .cavnarGreen)
                    Divider()
                    ledgerTile("\(ledger.noConsent)", "No consent", .cavnarEmber2)
                    Divider()
                    ledgerTile("\(ledger.unsubscribed)", "Unsubscribed", .cavnarInk3)
                }
                (Text("\(ledger.textsThisMonth)").font(.cavnarNumber(15, weight: 700))
                    + Text(" texts sent this month across ")
                    + Text("\(ledger.campaignsThisMonth)").font(.cavnarNumber(15, weight: 700))
                    + Text(" campaigns. Texts go out between \(ledger.window) only, and no guest gets two inside \(ledger.minDaysBetween) days."))
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .cavnarCard()
        }
    }

    private func ledgerTile(_ value: String, _ label: String, _ tint: Color) -> some View {
        VStack(spacing: 4) {
            Text(value).font(.cavnarNumber(22, weight: 500)).foregroundStyle(tint)
            Text(label).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }

    private var contactsCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    (Text("Guest contacts (") + Text("\(viewModel.contacts.count)").font(.cavnarNumber(16, weight: 700)) + Text(")"))
                        .font(.cavnarBody(16, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                    if !viewModel.contacts.isEmpty {
                        (Text("\(viewModel.textableCount)").font(.cavnarNumber(15, weight: 700))
                            + Text(" text-eligible"))
                            .font(.cavnarBody(15))
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
            // A failed action (a delete the server refused) is said above the
            // list rather than replacing it.
            if let error = viewModel.errorMessage {
                Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
            }
            if viewModel.isLoading {
                CavnarWorkingLine().padding(.vertical, 8)
            } else if viewModel.contacts.isEmpty && viewModel.errorMessage != nil {
                EmptyView()
            } else if viewModel.contacts.isEmpty {
                Text("Nobody has joined yet. Share the QR code above where guests can scan it.")
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                // Lazy: this is the one unbounded list in the app — a text
                // club is thousands of guests and get_guest_contacts applies
                // no LIMIT, so a plain ForEach in a ScrollView built every row
                // on appear (audit #17). Every other long list here is either
                // inside a List (Reviews) or bounded by its own data.
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(viewModel.contacts) { contact in
                        contactRow(contact)
                    }
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
                    .font(.cavnarBody(16, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Text(contact.phone).font(.cavnarNumber(15)).foregroundStyle(Color.cavnarInk3)
                Text(contact.statusLabel)
                    .font(.cavnarBody(15, weight: 700))
                    .foregroundStyle(statusColor(contact.status))
                if let visit = contact.lastVisit, !visit.isEmpty {
                    Text("Last visit \(shortDate(visit))")
                        .font(.cavnarBody(15))
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
                        .font(.cavnarBody(15, weight: 600))
                        .foregroundStyle(Color.cavnarEmber)
                }
                Button {
                    Haptic.selection()
                    contactToDelete = contact
                } label: {
                    Image(systemName: "trash").foregroundStyle(Color.cavnarEmber)
                }
                .accessibilityLabel("Delete \(contact.name?.isEmpty == false ? contact.name! : "guest")")
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
