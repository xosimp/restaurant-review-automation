import SwiftUI

/// Account → Refer a restaurant. The web's "Know another restaurant
/// owner?" card as its own sheet: one free month if they sign up. Lives
/// under Support, not inside the FAQ — it's something you do, not
/// something you read.
struct AccountReferralView: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var email = ""
    @State private var note = ""
    @State private var sending = false
    @State private var sent = false
    @State private var error: String?
    @FocusState private var focusedField: Field?

    private enum Field: Hashable, CaseIterable { case name, email, note }

    private var canSend: Bool { !sending && !name.trimmingCharacters(in: .whitespaces).isEmpty && email.contains("@") }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Refer a restaurant") {
                        GlowBadge(systemImage: "gift", size: 64)
                    } subtitle: {
                        Text("One free month when they sign up")
                    }

                    if sent {
                        VStack(spacing: 10) {
                            CavnarPostedCheck(label: "Referral sent") { dismiss() }
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.top, 20)
                    } else {
                        VStack(alignment: .leading, spacing: 8) {
                            AccountKicker(text: "Who should we introduce?")
                            VStack(alignment: .leading, spacing: 12) {
                                Text("Will sends them a short, personal intro from you — no pressure, no spam. If they become a client, your next month is on us.")
                                    .font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                                TextField("Restaurant or owner name", text: $name)
                                    .cavnarTextFieldStyle()
                                    .focused($focusedField, equals: .name)
                                    .submitLabel(.next)
                                    .onSubmit { focusedField = .email }
                                TextField("Their email", text: $email)
                                    .cavnarTextFieldStyle()
                                    .keyboardType(.emailAddress)
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()
                                    .focused($focusedField, equals: .email)
                                    .submitLabel(.next)
                                    .onSubmit { focusedField = .note }
                                TextField("A note from you (optional)", text: $note, axis: .vertical)
                                    .cavnarTextFieldStyle()
                                    .lineLimit(2...5)
                                    .focused($focusedField, equals: .note)
                                if let error {
                                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                                }
                                Button {
                                    focusedField = nil
                                    Task { await send() }
                                } label: {
                                    Group {
                                        if sending { CavnarShimmerText(text: "Sending…") } else { Text("Send referral") }
                                    }
                                    .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSend))
                                .disabled(!canSend)
                            }
                            .cavnarCard()
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Refer a restaurant")
            .keyboardNavToolbar($focusedField)
        }
    }

    private func send() async {
        sending = true
        error = nil
        let err = await viewModel.sendReferral(name: name.trimmingCharacters(in: .whitespaces),
                                               email: email.trimmingCharacters(in: .whitespaces), note: note)
        sending = false
        if let err {
            error = err
            Haptic.error()
        } else {
            sent = true
            Haptic.success()
        }
    }
}
