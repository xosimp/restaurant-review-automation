import SwiftUI
import Observation

@Observable
@MainActor
private final class SendReviewRequestViewModel {
    var isSending = false
    var errorMessage: String?
    var didSend = false
    /// GET /mobile/api/review-request-stats — the web modal's "Sent this
    /// month · All time" line, which the phone never read.
    var stats: Stats?

    struct Stats: Decodable, Equatable {
        let totalSent: Int
        let sentThisMonth: Int
        /// How many guests asked left a review within the window (memory
        /// round, 9/29/26: requests are matched to the reviews they
        /// brought). `pct` is nil below its floor — shown as "—".
        var conversion: RequestConversion? = nil
        enum CodingKeys: String, CodingKey {
            case conversion
            case totalSent = "total_sent"
            case sentThisMonth = "sent_this_month"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            totalSent = (try? c.decode(Int.self, forKey: .totalSent)) ?? 0
            sentThisMonth = (try? c.decode(Int.self, forKey: .sentThisMonth)) ?? 0
            conversion = (try? c.decodeIfPresent(RequestConversion.self, forKey: .conversion)) ?? nil
        }
    }

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

    func loadStats() async {
        stats = try? await client.send("/mobile/api/review-request-stats", hapticOnError: false)
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
                await loadStats()
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
    @State private var showingContacts = false
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
                    // A guest already in the phone's contacts, without
                    // retyping them (parity audit 10/7/26 #91). Only the one
                    // contact tapped comes back; a phone number from it is
                    // not consent — the toggle below still has to be set.
                    Button {
                        Haptic.light()
                        focusedField = nil
                        showingContacts = true
                    } label: {
                        Label("Choose from Contacts", systemImage: "person.crop.circle.badge.plus")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .background(
                        GuestContactPicker(isPresented: $showingContacts) { pick in
                            if !pick.name.isEmpty { name = pick.name }
                            email = pick.email
                            phone = pick.phone
                            // A new number has not agreed to anything.
                            smsConsent = false
                        }
                        .frame(width: 0, height: 0)
                    )

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
                        .cavnarText(.secondary)
                        .padding(.top, -14)

                    if !phone.trimmingCharacters(in: .whitespaces).isEmpty {
                        Toggle(isOn: $smsConsent) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("This guest agreed to be texted")
                                    .cavnarText(.label)
                                Text("Required before we send a review request by SMS.")
                                    .cavnarText(.caption)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .tint(Color.cavnarEmber)
                    }

                    if let stats = viewModel.stats {
                        HomeMixedText.make("Sent this month: \(stats.sentThisMonth) · All time: \(stats.totalSent)",
                                           role: .secondary)
                        // "3 of 10 guests you asked left a review within 14
                        // days (30%)" — matched request to review.
                        if let conversion = stats.conversion, conversion.asked > 0 {
                            CavnarMixedText(conversion.line, role: .secondary)
                        }
                    }

                    if let error = viewModel.errorMessage {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                    }

                    // Plain full-width buttons, not a width-matched pair —
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
            .task { await viewModel.loadStats() }
        }
    }
}
