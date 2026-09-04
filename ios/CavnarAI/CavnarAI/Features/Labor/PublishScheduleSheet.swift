import SwiftUI
import Observation

struct StaffContact: Decodable, Identifiable, Hashable {
    let employeeName: String
    let email: String
    let phone: String

    var id: String { employeeName }
    var isReachable: Bool { !email.isEmpty }

    enum CodingKeys: String, CodingKey {
        case employeeName = "employee_name"
        case email, phone
    }
}

/// Who was sent the schedule, and who has actually opened it — the
/// difference between "I sent it" and "the closing server has seen it".
struct ScheduleShareStatus: Decodable, Identifiable, Hashable {
    let employeeName: String
    let sentTo: String?
    let sentAt: String?
    let viewedAt: String?
    let viewCount: Int

    var id: String { employeeName }
    var hasViewed: Bool { viewedAt != nil }

    enum CodingKeys: String, CodingKey {
        case employeeName = "employee_name"
        case sentTo = "sent_to"
        case sentAt = "sent_at"
        case viewedAt = "viewed_at"
        case viewCount = "view_count"
    }
}

struct PublishResult: Decodable {
    let ok: Bool
    let sent: [Sent]
    let unreachable: [Unreachable]
    let failed: [Failed]
    let error: String?

    struct Sent: Decodable, Identifiable {
        let employeeName: String
        let sentTo: String
        let shifts: Int
        var id: String { employeeName }
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case sentTo = "sent_to"
            case shifts
        }
    }

    struct Unreachable: Decodable, Identifiable {
        let employeeName: String
        let reason: String
        var id: String { employeeName }
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case reason
        }
    }

    struct Failed: Decodable, Identifiable {
        let employeeName: String
        let error: String
        var id: String { employeeName }
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case error
        }
    }
}

@Observable
@MainActor
final class PublishScheduleViewModel {
    var contacts: [StaffContact] = []
    var status: [ScheduleShareStatus] = []
    var weekLabel: String?
    var isLoading = false
    var errorMessage: String?

    var isPublishing = false
    var lastResult: PublishResult?

    var editingContact: StaffContact?
    var isSavingContact = false
    var contactError: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct ContactsResponse: Decodable {
        let ok: Bool
        let contacts: [StaffContact]
        let reachable: Int
        let weekStart: String?
        let weekEnd: String?

        enum CodingKeys: String, CodingKey {
            case ok, contacts, reachable
            case weekStart = "week_start"
            case weekEnd = "week_end"
        }
    }

    private struct StatusResponse: Decodable { let ok: Bool; let status: [ScheduleShareStatus] }
    private struct OKErrorResponse: Decodable { let ok: Bool; let error: String? }

    var reachableCount: Int { contacts.filter(\.isReachable).count }

    func load() async {
        isLoading = contacts.isEmpty
        errorMessage = nil
        defer { isLoading = false }
        do {
            let r: ContactsResponse = try await client.send("/mobile/api/labor/staff-contacts")
            contacts = r.contacts
            if let start = r.weekStart {
                weekLabel = r.weekEnd.map { "\(start) – \($0)" } ?? start
            }
            let s: StatusResponse? = try? await client.send(
                "/mobile/api/labor/schedule-share-status", hapticOnError: false)
            status = s?.status ?? []
        } catch let error as APIClient.APIError {
            if contacts.isEmpty { errorMessage = error.message }
        } catch is CancellationError {
            // View went away mid-fetch; not a failure.
        } catch {
            if contacts.isEmpty { errorMessage = "Couldn't load your staff list." }
        }
    }

    private struct ContactBody: Encodable {
        let employeeName: String
        let email: String
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case email
        }
    }

    @discardableResult
    func saveContact(_ name: String, email: String) async -> Bool {
        isSavingContact = true
        contactError = nil
        defer { isSavingContact = false }
        do {
            let r: OKErrorResponse = try await client.send(
                "/mobile/api/labor/staff-contacts", method: .post,
                body: ContactBody(employeeName: name, email: email))
            if r.ok {
                await load()
                return true
            }
            contactError = r.error ?? "Couldn't save that."
            return false
        } catch let error as APIClient.APIError {
            contactError = error.message
            return false
        } catch {
            contactError = "Couldn't save that."
            return false
        }
    }

    func publish() async {
        guard !isPublishing else { return }
        isPublishing = true
        defer { isPublishing = false }
        do {
            let result: PublishResult = try await client.send(
                "/mobile/api/labor/publish-schedule", method: .post)
            lastResult = result
            if result.ok { Haptic.success() }
            await load()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't send the schedule."
        }
    }
}

/// Sends each member of staff their own shifts. The labor module generated
/// schedules and stopped at a CSV download — the people who actually work
/// the shifts never saw it.
struct PublishScheduleSheet: View {
    @State private var viewModel = PublishScheduleViewModel()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    if viewModel.isLoading && viewModel.contacts.isEmpty {
                        CavnarSkeletonLines(widths: [1.0, 0.8, 0.6, 0.45])
                    } else if let error = viewModel.errorMessage, viewModel.contacts.isEmpty {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
                    } else if viewModel.contacts.isEmpty {
                        emptyState
                    } else {
                        if let result = viewModel.lastResult {
                            resultCard(result)
                        }
                        staffCard
                        publishButton
                        if !viewModel.status.isEmpty {
                            statusCard
                        }
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Send to staff")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Send to staff")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        dismiss()
                    } label: {
                        Text("Done").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .task { await viewModel.load() }
            .sheet(item: $viewModel.editingContact) { contact in
                StaffContactSheet(viewModel: viewModel, contact: contact)
            }
        }
    }

    private var staffCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .lastTextBaseline) {
                Text("THIS WEEK'S STAFF")
                    .font(.cavnarBody(13.5, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                Spacer()
                HomeMixedText.make("\(viewModel.reachableCount) of \(viewModel.contacts.count) reachable",
                                   size: 12.5, weight: 700, color: .cavnarInk3)
            }
            if let week = viewModel.weekLabel {
                Text(week).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
            }
            VStack(spacing: 0) {
                ForEach(Array(viewModel.contacts.enumerated()), id: \.element.id) { index, contact in
                    Button {
                        Haptic.light()
                        viewModel.editingContact = contact
                    } label: {
                        HStack(spacing: 10) {
                            Circle()
                                .fill(contact.isReachable ? Color.cavnarGreen : Color.cavnarAmber)
                                .frame(width: 7, height: 7)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(contact.employeeName)
                                    .font(.cavnarBody(15, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                Text(contact.isReachable ? contact.email : "No email yet — tap to add")
                                    .font(.cavnarBody(13))
                                    .foregroundStyle(contact.isReachable ? Color.cavnarInk3 : Color.cavnarAmber)
                            }
                            Spacer(minLength: 8)
                            Image(systemName: "chevron.right")
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                        .padding(.vertical, 11)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    if index < viewModel.contacts.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
            Text("Everyone gets a private link to their own shifts only — no logins, nothing else on the roster.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .cavnarCard()
    }

    private var publishButton: some View {
        Button {
            Task { await viewModel.publish() }
        } label: {
            Group {
                if viewModel.isPublishing {
                    CavnarShimmerText(text: "Sending…")
                } else {
                    Text(viewModel.reachableCount > 0
                         ? "Send to \(viewModel.reachableCount) staff"
                         : "Add an email address first")
                }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.reachableCount == 0 || viewModel.isPublishing))
        .disabled(viewModel.reachableCount == 0 || viewModel.isPublishing)
    }

    private func resultCard(_ result: PublishResult) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(result.sent) { sent in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarGreen)
                    HomeMixedText.make("\(sent.employeeName) — \(sent.shifts) shifts sent",
                                       size: 14, weight: 600, color: .cavnarInk)
                }
            }
            ForEach(result.unreachable) { person in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "exclamationmark.circle.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarAmber)
                    Text("\(person.employeeName) — \(person.reason)")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(result.failed) { failure in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarRed)
                    Text("\(failure.employeeName) — \(failure.error)")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("WHO'S SEEN IT")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 0) {
                ForEach(Array(viewModel.status.enumerated()), id: \.element.id) { index, row in
                    HStack(spacing: 10) {
                        Text(row.employeeName)
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk)
                        Spacer(minLength: 8)
                        if row.hasViewed {
                            HomeMixedText.make(row.viewCount > 1 ? "Opened \(row.viewCount) times" : "Opened",
                                               size: 13, weight: 700, color: .cavnarGreen)
                        } else {
                            Text("Not opened yet")
                                .font(.cavnarBody(13, weight: 600))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    .padding(.vertical, 10)
                    if index < viewModel.status.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
        .cavnarCard()
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("No schedule yet")
                .font(.cavnarHeadline(17))
                .foregroundStyle(Color.cavnarInk)
            Text("Generate next week's schedule first — then you can send everyone their own shifts.")
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .cavnarCard()
    }
}

private enum StaffContactField: Hashable, CaseIterable { case email }

private struct StaffContactSheet: View {
    let viewModel: PublishScheduleViewModel
    let contact: StaffContact

    @Environment(\.dismiss) private var dismiss
    @State private var email = ""
    @FocusState private var focusedField: StaffContactField?

    private var canSubmit: Bool {
        !viewModel.isSavingContact && (email.isEmpty || email.contains("@"))
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("Where should \(contact.employeeName)'s schedule go? They'll get a private link to their own shifts — no login needed.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    CavnarFloatingField(
                        icon: "envelope", placeholder: "Email address", text: $email,
                        autocapitalization: .never, focus: $focusedField, field: .email
                    )

                    if let error = viewModel.contactError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    VStack(spacing: 10) {
                        Button {
                            Task {
                                if await viewModel.saveContact(contact.employeeName, email: email) {
                                    dismiss()
                                }
                            }
                        } label: {
                            Group {
                                if viewModel.isSavingContact {
                                    CavnarShimmerText(text: "Saving…")
                                } else {
                                    Text("Save")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit))
                        .disabled(!canSubmit)

                        Button { dismiss() } label: {
                            Text("Cancel").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle(contact.employeeName)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar(contact.employeeName) }
            .keyboardNavToolbar($focusedField)
        }
        .onAppear {
            email = contact.email
            focusedField = .email
        }
    }
}
