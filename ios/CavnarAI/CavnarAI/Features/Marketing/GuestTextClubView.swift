import SwiftUI

private enum CampaignField: Hashable, CaseIterable {
    case topic, draftMessage
}

struct GuestTextClubView: View {
    @State private var viewModel = GuestTextClubViewModel()
    @State private var showingAddContact = false
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
        VStack(alignment: .leading, spacing: 6) {
            Text("Guest join link").font(.cavnarBody(14, weight: 700)).foregroundStyle(Color.cavnarInk3)
            Text(url)
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk)
                .textSelection(.enabled)
        }
        .cavnarCard()
    }

    private var campaignCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Send a campaign").font(.cavnarBody(14.5, weight: 700)).foregroundStyle(Color.cavnarInk)

            CavnarSegmentedControl(
                selection: $viewModel.campaignType,
                options: ["general", "promo", "event"]
            ) { type in
                switch type {
                case "promo": return "Promo"
                case "event": return "Event"
                default: return "General"
                }
            }

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
                    CavnarInlinePosted(label: "Campaign sent") {
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
                (Text("Guest contacts (") + Text("\(viewModel.contacts.count)").font(.cavnarNumber(14.5, weight: 700)) + Text(")"))
                    .font(.cavnarBody(14.5, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
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
            } else {
                ForEach(viewModel.contacts) { contact in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(contact.name?.isEmpty == false ? contact.name! : "Guest")
                                .font(.cavnarBody(14.5, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            Text(contact.phone).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                        }
                        Spacer()
                        if contact.consent == true {
                            Text("Consented").font(.cavnarBody(14, weight: 700)).foregroundStyle(Color.cavnarGreen)
                        }
                        Button {
                            Haptic.selection()
                            Task { await viewModel.deleteContact(contact) }
                        } label: {
                            Image(systemName: "trash").foregroundStyle(Color.cavnarRed)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
        }
        .cavnarCard()
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
