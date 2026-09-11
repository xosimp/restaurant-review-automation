import SwiftUI
import Observation

@Observable
@MainActor
private final class SendReviewRequestViewModel {
    var isSending = false
    var errorMessage: String?
    var didSend = false

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct Body: Encodable {
        let name: String
        let email: String
        let phone: String
        let message: String
        // Every other outbound SMS in this product goes only to a contact
        // who consented. A review request texted whatever number was typed
        // in, with nothing recording that the guest agreed to it.
        let sms_consent: Bool
    }

    private struct Response: Decodable {
        let ok: Bool
        let error: String?
    }

    func send(name: String, email: String, phone: String, message: String,
              smsConsent: Bool) async {
        isSending = true
        errorMessage = nil
        defer { isSending = false }
        do {
            let response: Response = try await client.send(
                "/mobile/api/send-review-request", method: .post,
                body: Body(name: name, email: email, phone: phone, message: message,
                           sms_consent: smsConsent)
            )
            if response.ok {
                didSend = true
            } else {
                errorMessage = response.error ?? "Couldn't send that request."
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't send that request."
        }
    }
}

private enum SendReviewRequestField: Hashable, CaseIterable {
    case name, email, phone, message
}

struct SendReviewRequestSheet: View {
    @State private var viewModel = SendReviewRequestViewModel()
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var email = ""
    @State private var phone = ""
    @State private var message = ""
    @State private var smsConsent = false
    @FocusState private var focusedField: SendReviewRequestField?
    // Set only on the real 200 — the posted check plays, then the sheet
    // closes itself (see cavnarPostedOverlay).
    @State private var postedLabel: String?

    private var canSend: Bool {
        if viewModel.isSending { return false }
        if email.isEmpty && phone.isEmpty { return false }
        // A phone number without consent has nowhere to go — the server
        // refuses it, so don't let the button pretend otherwise.
        if !phone.trimmingCharacters(in: .whitespaces).isEmpty && !smsConsent { return false }
        return true
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    CavnarFloatingField(
                        icon: "person", placeholder: "Guest name", text: $name, textContentType: .name,
                        focus: $focusedField, field: .name
                    )
                    CavnarFloatingField(
                        icon: "envelope", placeholder: "Email", text: $email,
                        keyboardType: .emailAddress, textContentType: .emailAddress, autocapitalization: .never,
                        focus: $focusedField, field: .email
                    )
                    CavnarFloatingField(
                        icon: "phone", placeholder: "Phone (optional)", text: $phone, keyboardType: .phonePad,
                        focus: $focusedField, field: .phone
                    )

                    CavnarFloatingTextArea(
                        caption: "Note to guest — optional",
                        placeholder: "e.g. Thanks for celebrating your anniversary with us!",
                        text: $message,
                        focus: $focusedField, field: .message
                    )
                    Text("Sent along with the review link. Leave it blank to use the default message.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .padding(.top, -14)

                    if !phone.trimmingCharacters(in: .whitespaces).isEmpty {
                        Toggle(isOn: $smsConsent) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("This guest agreed to be texted")
                                    .font(.cavnarBody(15, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                Text("Required before we send a review request by SMS.")
                                    .font(.cavnarBody(13.5))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .tint(Color.cavnarEmber)
                    }

                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    }

                    // Plain full-width buttons, not CavnarFormButtonPair —
                    // that PreferenceKey width-matching mechanism doesn't
                    // reliably resolve when the sheet sits in this app's
                    // deeper sheet-presentation chains (already root-caused
                    // and fixed the same way in TwoFactorSetupSheet and
                    // AccountSecurityDetailView's password form; this sheet
                    // just hadn't been migrated yet).
                    VStack(spacing: 10) {
                        Button {
                            Task {
                                await viewModel.send(name: name, email: email, phone: phone,
                                                     message: message, smsConsent: smsConsent)
                                if viewModel.didSend {
                                    Haptic.success()
                                    postedLabel = "Review request sent"
                                }
                            }
                        } label: {
                            Group {
                                if viewModel.isSending {
                                    CavnarShimmerText(text: "Sending…", color: Color.cavnarInk)
                                } else {
                                    Text("Send review request")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSend))
                        .disabled(!canSend)

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
            .navigationTitle("Request a Review")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Request a Review") }
            .keyboardNavToolbar($focusedField)
            .cavnarPostedOverlay(postedLabel) { dismiss() }
        }
    }
}
