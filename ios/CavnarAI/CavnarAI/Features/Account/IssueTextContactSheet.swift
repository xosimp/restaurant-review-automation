import SwiftUI

/// The web's "Add someone to text" (iOS parity #44): a name, a mobile
/// number, and the owner's record that the person agreed to operational
/// texts — then issues are routed to them (POST /issues/routing/contact,
/// strategy_routes._do_routing_contact). They join the account's alert
/// contacts, at most two people. Nothing is texted here.
struct IssueTextContactSheet: View {
    var onSaved: (AccountAlertsDetailView.IssueRouting) -> Void
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focus: Field?
    @State private var name = ""
    @State private var phone = ""
    @State private var role = "manager"
    @State private var consent = false
    @State private var busy = false
    @State private var error: String?

    enum Field: Hashable { case name, phone }

    /// Every key _do_routing_contact reads.
    struct ContactBody: Encodable, Equatable {
        let role: String
        let name: String
        let phone: String
        let consent: Bool
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    AccountSection(kicker: "Who to text") {
                        AccountField(label: "Their name", text: $name, focus: $focus, field: .name)
                        AccountField(label: "Mobile number", text: $phone, focus: $focus, field: .phone,
                                     keyboardType: .phonePad, isNumber: true, showsDivider: false)
                            .onChange(of: phone) { _, v in let f = PhoneFormat.typing(v); if f != v { phone = f } }
                    }
                    AccountSection(kicker: "Send them") {
                        CavnarSegmentedControl(selection: $role, options: ["manager", "escalation"]) {
                            $0 == "manager" ? "New issues" : "Escalations"
                        }
                        .padding(.vertical, 9)
                    }
                    AccountSection(kicker: "Consent") {
                        AccountSwitchRow(
                            label: "They agreed to texts",
                            detail: "They agreed to receive operational texts from Cavnar AI. Message & data rates may apply; reply STOP to cancel, HELP for help.",
                            isOn: $consent, showsDivider: false
                        )
                    }
                    if let error {
                        Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Button {
                        focus = nil
                        Task { await save() }
                    } label: {
                        Group {
                            if busy { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Add and send issues to them") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy || !canSave))
                    .disabled(busy || !canSave)
                }
                .padding(20)
            }
            .accountSheetChrome("Add Someone")
            .keyboardDoneToolbar { focus = nil }
        }
    }

    private var canSave: Bool {
        !name.trimmingCharacters(in: .whitespaces).isEmpty && phone.filter(\.isNumber).count >= 10 && consent
    }

    private func save() async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            let updated: AccountAlertsDetailView.IssueRouting = try await APIClient.shared.send(
                "/mobile/api/issues/routing/contact", method: .post,
                body: ContactBody(role: role, name: name.trimmingCharacters(in: .whitespaces), phone: phone, consent: consent),
                retryTransient: false)
            Haptic.success()
            onSaved(updated)
            dismiss()
        } catch let e as APIClient.APIError {
            error = e.message
            Haptic.error()
        } catch {
            self.error = "Couldn't add them."
            Haptic.error()
        }
    }
}
