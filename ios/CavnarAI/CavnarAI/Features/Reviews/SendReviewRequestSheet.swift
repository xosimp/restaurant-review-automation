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
    }

    private struct Response: Decodable {
        let ok: Bool
        let error: String?
    }

    func send(name: String, email: String, phone: String) async {
        isSending = true
        errorMessage = nil
        defer { isSending = false }
        do {
            let response: Response = try await client.send(
                "/mobile/api/send-review-request", method: .post,
                body: Body(name: name, email: email, phone: phone)
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

    var body: some View {
        NavigationStack {
            Form {
                Section("Guest details") {
                    TextField("Name", text: $name)
                    TextField("Email", text: $email)
                        .keyboardType(.emailAddress)
                        .autocapitalization(.none)
                    TextField("Phone", text: $phone)
                        .keyboardType(.phonePad)
                }
                if let error = viewModel.errorMessage {
                    Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                }
                Section {
                    Button {
                        Task {
                            await viewModel.send(name: name, email: email, phone: phone)
                            if viewModel.didSend { dismiss() }
                        }
                    } label: {
                        if viewModel.isSending {
                            HStack { Spacer(); ProgressView(); Spacer() }
                        } else {
                            Text("Send review request")
                        }
                    }
                    .disabled(viewModel.isSending || (email.isEmpty && phone.isEmpty))
                }
            }
            .scrollContentBackground(.hidden)
            .background(Color.cavnarPaper)
            .navigationTitle("Request a Review")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}
