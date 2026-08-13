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
    }

    private struct Response: Decodable {
        let ok: Bool
        let error: String?
    }

    func send(name: String, email: String, phone: String, message: String) async {
        isSending = true
        errorMessage = nil
        defer { isSending = false }
        do {
            let response: Response = try await client.send(
                "/mobile/api/send-review-request", method: .post,
                body: Body(name: name, email: email, phone: phone, message: message)
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

struct SendReviewRequestSheet: View {
    @State private var viewModel = SendReviewRequestViewModel()
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var email = ""
    @State private var phone = ""
    @State private var message = ""

    private var canSend: Bool {
        !viewModel.isSending && !(email.isEmpty && phone.isEmpty)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    CavnarFloatingField(icon: "person", placeholder: "Guest name", text: $name, textContentType: .name)
                    CavnarFloatingField(
                        icon: "envelope", placeholder: "Email", text: $email,
                        keyboardType: .emailAddress, textContentType: .emailAddress, autocapitalization: .never
                    )
                    CavnarFloatingField(icon: "phone", placeholder: "Phone (optional)", text: $phone, keyboardType: .phonePad)

                    CavnarFloatingTextArea(
                        caption: "Note to guest — optional",
                        placeholder: "e.g. Thanks for celebrating your anniversary with us!",
                        text: $message
                    )
                    Text("Sent along with the review link. Leave it blank to use the default message.")
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                        .padding(.top, -14)

                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                    }

                    VStack(spacing: 10) {
                        Button {
                            Task {
                                await viewModel.send(name: name, email: email, phone: phone, message: message)
                                if viewModel.didSend { dismiss() }
                            }
                        } label: {
                            if viewModel.isSending {
                                ProgressView().tint(Color.cavnarInk)
                            } else {
                                Text("Send review request")
                            }
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSend))
                        .disabled(!canSend)

                        Button("Cancel") { dismiss() }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                    .padding(.top, 6)
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Request a Review")
            .navigationBarTitleDisplayMode(.inline)
            .cavnarEmberTitle("Request a Review")
        }
        .cavnarSheetTopRim()
    }
}
